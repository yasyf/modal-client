import asyncio
import os

import grpc
import grpc.aio

from .async_utils import TaskContext, retry, synchronizer
from .config import config, logger
from .exception import AuthError, ConnectionError, InvalidError
from .grpc_utils import BLOCKING_REQUEST_TIMEOUT, GRPC_REQUEST_TIMEOUT, ChannelPool
from .proto import api_pb2, api_pb2_grpc
from .server_connection import GRPCConnectionFactory


@synchronizer
class Client:
    def __init__(
        self,
        server_url,
        client_type,
        credentials,
    ):
        self.server_url = server_url
        self.client_type = client_type
        self.credentials = credentials
        self._task_context = None
        self._channel_pool = None

    async def _start(self):
        logger.debug("Client: Starting")
        self.stopped = asyncio.Event()
        self._task_context = TaskContext()
        await self._task_context.start()
        self._connection_factory = GRPCConnectionFactory(
            self.server_url,
            self.client_type,
            self.credentials,
        )
        self._channel_pool = ChannelPool(self._task_context, self._connection_factory)
        await self._channel_pool.start()
        self.stub = api_pb2_grpc.PolyesterClientStub(self._channel_pool)
        try:
            req = api_pb2.ClientCreateRequest(client_type=self.client_type)
            resp = await self.stub.ClientCreate(req)
            self.client_id = resp.client_id
        except grpc.aio._call.AioRpcError as exc:
            if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise AuthError(f"Connecting to {self.server_url}: {exc.details()}")
            elif exc.code() == grpc.StatusCode.UNAVAILABLE:
                raise ConnectionError(f"Connecting to {self.server_url}: {exc.details()}")
            else:
                raise
        if not self.client_id:
            raise InvalidError("Did not get a client id from server")

        # Start heartbeats
        self._task_context.infinite_loop(self._heartbeat, sleep=3.0)

        logger.debug("Client: Done starting")

    async def _stop(self):
        # TODO: we should trigger this using an exit handler
        logger.debug("Client: Shutting down")
        if self._task_context:
            await self._task_context.stop()
        if self._channel_pool:
            await self._channel_pool.close()
        logger.debug("Client: Done shutting down")
        # Needed to catch straggling CancelledErrors and GeneratorExits that propagate
        # through our chains of async generators.
        await asyncio.sleep(0.01)

    async def _heartbeat(self):
        req = api_pb2.ClientHeartbeatRequest(client_id=self.client_id)
        await self.stub.ClientHeartbeat(req)

    async def __aenter__(self):
        try:
            await self._start()
        except:
            await self._stop()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._stop()

    @classmethod
    async def from_env(cls):
        server_url = config["server.url"]
        token_id = config["token.id"]
        token_secret = config["token.secret"]
        task_id = config["task.id"]
        task_secret = config["task.secret"]

        if task_id and task_secret:
            client_type = api_pb2.ClientType.CONTAINER
            credentials = (task_id, task_secret)
        elif token_id and token_secret:
            client_type = api_pb2.ClientType.CLIENT
            credentials = (token_id, token_secret)
        else:
            client_type = api_pb2.ClientType.CLIENT
            credentials = None

        client = Client(server_url, client_type, credentials)
        return client
