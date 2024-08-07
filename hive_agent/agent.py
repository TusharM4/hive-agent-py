import asyncio
import logging
import signal
import sys
import uvicorn
import os

from typing import Callable, List, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from llama_index.agent.openai import OpenAIAgent
from llama_index.core.agent import FunctionCallingAgentWorker

from hive_agent.llms import OpenAILLM
from hive_agent.llms import ClaudeLLM
from hive_agent.llms import MistralLLM
from hive_agent.llms import OllamaLLM

from llama_index.core.llms import ChatMessage
from llama_index.core.tools import FunctionTool

from hive_agent.llm_settings import init_llm_settings
from hive_agent.server.routes import setup_routes, files
from hive_agent.tools.agent_db import get_db_schemas, text_2_sql, basic_retrieve

from dotenv import load_dotenv
from hive_agent.config import Config

load_dotenv()


class HiveAgent:
    name: str
    wallet_store: "WalletStore"  # this attribute will be conditionally initialized
    __agent: Any

    def __init__(
        self,
        name: str,
        functions: List[Callable],
        config_path="../../hive_config_example.toml",
        host="0.0.0.0",
        port=8000,
        instruction="",
        role="",
        retrieve=False,
        required_exts=[".md", '.mdx' ,".txt", '.csv', '.docx', '.pdf'],
    ):
        self.name = name
        self.functions = functions
        self.config_path = config_path
        self.host = host
        self.port = port
        self.app = FastAPI()
        self.shutdown_event = asyncio.Event()
        self.instruction = instruction
        self.__role__ = role
        self.optional_dependencies = {}
        self.config = Config(config_path=config_path)
        self.retrieve = retrieve
        self.required_exts = required_exts
        logging.basicConfig(stream=sys.stdout, level=self.config.get_log_level())
        logging.getLogger().addHandler(logging.StreamHandler(stream=sys.stdout))

        self.logger = logging.getLogger()
        self.logger.setLevel(self.config.get_log_level())

        self._check_optional_dependencies()
        self.__setup()

    def _check_optional_dependencies(self):
        try:
            from web3 import Web3

            self.optional_dependencies["web3"] = True
        except ImportError:
            self.optional_dependencies["web3"] = False

    def __setup(self):
        init_llm_settings(self.config)
        custom_tools = self._tools_from_funcs(self.functions)

        # TODO: pass db client to db tools directly
        system_tools = self._tools_from_funcs([get_db_schemas, text_2_sql])

        tools = custom_tools + system_tools

        is_base_dir_not_empty = lambda: os.path.exists(files.BASE_DIR) and (
            os.path.getsize(files.BASE_DIR) > 0
            if os.path.isfile(files.BASE_DIR)
            else (
                bool(os.listdir(files.BASE_DIR))
                if os.path.isdir(files.BASE_DIR)
                else False
            )
        )

        tool_retriever = None

        if is_base_dir_not_empty() == True & self.retrieve == True:
            tool_retriever = basic_retrieve(tools, self.required_exts)
            tools = []  # Cannot specify both tools and tool_retriever

        model = self.config.get("model", "model", "gpt-3.5-turbo")
        if "gpt" in model:
            self.__agent = OpenAILLM(tools, self.instruction, tool_retriever).agent
        elif "claude" in model:
            self.__agent = ClaudeLLM(tools, self.instruction, tool_retriever).agent
        elif "llama" in model:
            self.__agent = OllamaLLM(tools, self.instruction, tool_retriever).agent
        elif "mixtral" or "mistral" in model:
            self.__agent = MistralLLM(tools, self.instruction, tool_retriever).agent
        else:
            self.__agent = OpenAILLM(tools, self.instruction, tool_retriever).agent

        if self.optional_dependencies.get("web3"):
            from hive_agent.wallet import WalletStore

            self.wallet_store = WalletStore()
            self.wallet_store.add_wallet()
        else:
            self.wallet_store = None
            self.logger.warning(
                "'web3' extras not installed. Web3-related functionality will not be available."
            )

        self.__setup_server()

    @staticmethod
    def _tools_from_funcs(funcs: List[Callable]) -> List[FunctionTool]:
        return [FunctionTool.from_defaults(fn=func) for func in funcs]

    def __setup_server(self):

        self.configure_cors()
        setup_routes(self.app, self.__agent)

        signal.signal(signal.SIGINT, self.__signal_handler)
        signal.signal(signal.SIGTERM, self.__signal_handler)

    def configure_cors(self):
        environment = self.config.get(
            "environment", "type"
        )  # default to 'development' if not set

        if environment == "dev":
            logger = logging.getLogger("uvicorn")
            logger.warning(
                "Running in development mode - allowing CORS for all origins"
            )
            self.app.add_middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )

    async def run_server(self):
        try:
            config = uvicorn.Config(
                app=self.app, host=self.host, port=self.port, loop="asyncio"
            )
            server = uvicorn.Server(config)
            await server.serve()
        except Exception as e:
            logging.error(
                f"unexpected error while running the server: {e}", exc_info=True
            )
        finally:
            await self.__cleanup()

    def run(self):
        try:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(self.run_server())
        except Exception as e:
            logging.error(
                f"An error occurred in the main event loop: {e}", exc_info=True
            )

    def chat_history(self) -> List[ChatMessage]:
        return self.__agent.chat_history

    @staticmethod
    def _tools_from_funcs(funcs: List[Callable]) -> List[FunctionTool]:
        return [FunctionTool.from_defaults(fn=func) for func in funcs]

    def __signal_handler(self, signum, frame):
        logging.info(f"signal {signum} received, initiating graceful shutdown...")
        asyncio.create_task(self.shutdown_procedures())

    async def shutdown_procedures(self):
        # attempt to complete or cancel all running tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [task.cancel() for task in tasks]

        await asyncio.gather(*tasks, return_exceptions=True)
        self.shutdown_event.set()
        logging.info("all tasks have been cancelled or completed")

    async def __cleanup(self):
        try:
            if hasattr(self, "db_session"):
                await self.db_session.close()
                logging.debug("database connection closed")
        except Exception as e:
            logging.error(f"error during cleanup: {e}", exc_info=True)
        finally:
            logging.info("cleanup process completed")
