import logging
from typing import Set

from crewai import Agent, Task, Crew
from socketio.exceptions import ConnectionError as ConnError
from socketio import SimpleClient

import models.mongo
from utils.model_helper import keyset
from init.env_variables import AGENT_BACKEND_SOCKET_TOKEN, QDRANT_HOST, SOCKET_URL
from typing import Dict
from langchain_openai.chat_models import ChatOpenAI, AzureChatOpenAI
from langchain_community.embeddings.fastembed import FastEmbedEmbeddings
from langchain_core.agents import AgentFinish
from models.sockets import SocketMessage, SocketEvents, Message
from langchain_community.vectorstores.qdrant import Qdrant
from qdrant_client import QdrantClient
from tools import RagToolFactory
from messaging.send_message_to_socket import send


class CrewAIBuilder:

    def __init__(
            self,
            session_id: str,
            crew: Crew,
            agents: Dict[Set[models.mongo.PyObjectId], models.mongo.Agent],
            tasks: Dict[Set[models.mongo.PyObjectId], models.mongo.Task],
            tools: Dict[Set[models.mongo.PyObjectId], models.mongo.Tool],
            datasources: Dict[Set[models.mongo.PyObjectId], models.mongo.Datasource],
            models: Dict[Set[models.mongo.PyObjectId], models.mongo.Model],
            credentials: Dict[Set[models.mongo.PyObjectId], models.mongo.Credentials]
    ):
        self.session_id = session_id
        self.crew_model = crew
        self.agents_models = agents
        self.tasks_models = tasks
        self.tools_models = tools
        self.datasources_models = datasources
        self.models_models = models
        self.credentials_models = credentials
        self.crew = None
        self.crew_models = dict()
        self.crew_tools = dict()
        self.crew_agents = dict()
        self.crew_tasks = dict()
        self.init_socket()

    def init_socket(self):
        try:
            ## Initialize the socket client and connect
            self.socket = SimpleClient()
            custom_headers = {"x-agent-backend-socket-token": AGENT_BACKEND_SOCKET_TOKEN}
            print(f"Socker URL:   {SOCKET_URL}")
            self.socket.connect(url=SOCKET_URL, headers=custom_headers)
            self.socket.emit("join_room", f"_{self.session_id}")
        except ConnError as ce:
            logging.error(f"Connection error occurred: {ce}")
            raise

    @staticmethod
    def match_key(elements_dict: Dict[Set[str], any], key: Set[str], exact=False):
        for k, v in elements_dict.items():
            if exact and key == k:
                return v
            elif key.issubset(k):
                return v
        return None

    @staticmethod
    def search_subordinate_keys(elements_dict: Dict[Set[str], any], key: Set[str]):
        results = dict()
        for k, v in elements_dict.items():
            if key.issubset(k) and key != k:
                results[k] = v
        return results

    def build_models_with_credentials(self):
        for key, model in self.models_models.items():
            credential = self.match_key(self.credentials_models, key)
            if credential:
                match credential.type:
                    case models.mongo.Platforms.ChatOpenAI:
                        self.crew_models[key] = ChatOpenAI(
                            api_key=credential.credentials.api_key,
                            **model.model_dump(
                                exclude_none=True,
                                exclude_unset=True,
                                exclude=["id", "credentialId",
                                         "embeddingLength"]
                            )
                        )
                    case models.mongo.Platforms.AzureChatOpenAI:
                        self.crew_models[key] = AzureChatOpenAI(
                            api_key=credential.credentials.api_key,
                            **model.model_dump(
                                exclude_none=True,
                                exclude_unset=True,
                                exclude=["id", "credentialId",
                                         "embeddingLength"]
                            )
                        )
                    case models.mongo.Platforms.FastEmbed:
                        self.crew_models[key] = FastEmbedEmbeddings(
                            **model.model_dump(exclude_none=True, exclude_unset=True,
                                               exclude=["id", "name", "embeddingLength"]))

    def build_tools_and_their_datasources(self):
        for key, tool in self.tools_models.items():
            datasource = self.match_key(self.datasources_models, key)
            if datasource:
                embedding_model = self.match_key(self.crew_models, key)
                # Avoid the model_name conversion in FastEmbed models instantiation
                embedding_model_model = self.match_key(self.models_models, key)
                if embedding_model:
                    tool_factory = RagToolFactory()
                    collection = str(datasource.id)
                    tool_factory.init(
                        Qdrant(
                            QdrantClient(QDRANT_HOST),
                            collection_name=collection,
                            embeddings=embedding_model,
                            vector_name=embedding_model_model.model_name
                        ),
                        embedding_model
                    )
                    self.crew_tools[key] = tool_factory.generate_langchain_tool(tool.name, tool.description)

    def build_agents(self):
        for key, agent in self.agents_models.items():
            model_obj = self.match_key(self.crew_models, key, exact=True)
            agent_tools_objs = self.search_subordinate_keys(self.crew_tools, key)
            self.crew_agents[key] = Agent(
                **agent.model_dump(
                    exclude_none=True, exclude_unset=True,
                    exclude=["id", "toolIds", "modelId", "taskIds"]
                ),
                llm=model_obj, tools=agent_tools_objs.values()
            )

    def build_tasks(self):
        for key, task in self.tasks_models.items():
            agent_obj = self.match_key(self.crew_agents, keyset(task.agentId), exact=True)
            task_tools_objs = self.search_subordinate_keys(self.crew_tools, key)
            self.crew_tasks[key] = Task(
                **task.model_dump(exclude_none=True, exclude_unset=True, exclude=["id"]),
                agent=agent_obj, tools=task_tools_objs.values()
            )

    def build_crew(self):
        # 1. Build llm/embedding model from Model + Credentials
        self.build_models_with_credentials()

        # 2. Build Crew-Tool from Tool + llm/embedding (#1) + Model (TBD) + Datasource (optional)
        self.build_tools_and_their_datasources()

        # 3. Build Crew-Agent from Agent + llm/embedding (#1) + Crew-Tool (#2)
        self.build_agents()

        # 4. Build Crew-Task from Task + Crew-Agent (#3) + Crew-Tool (#2)
        self.build_tasks()

        # 5. Build Crew-Crew from Crew + Crew-Task (#4) + Crew-Agent (#3)
        self.crew = Crew(
            agents=self.crew_agents.values(), tasks=self.crew_tasks.values(),
            **self.crew_model.model_dump(
                exclude_none=True, exclude_unset=True,
                exclude=["id", "tasks", "agents"]),
            step_callback=self.send_it
        )

    def send_it(self, message):
        try:
            message_type = type(message)
            if message_type is AgentFinish:
                if hasattr(message, "return_values"):
                    socket_message = SocketMessage(
                        room=self.session_id,
                        authorName="system",
                        message=Message(
                            text=message.return_values.get('output'),
                            tokens=1,
                            first=True,
                        )
                    )
                    send(self.socket, SocketEvents.MESSAGE, socket_message, "both")
            elif message_type is list or message_type is tuple:
                for message_part in message:
                    self.send_it(message_part)
            else:
                print("FAILED TO PROCESS", message_type, message)
        except Exception as e:
            logging.exception(e)

    def run_crew(self):
        try:
            self.crew.kickoff()
        except Exception as e:
            logging.exception(e)
