from typing import Optional
from nats.js.client import JetStreamContext
from nats.aio.client import Client as NATS
from pydantic import TypeAdapter

from perpetual_os1.robot import RobotCommands
from perpetual_os1.robot_defs import WorkerStatus
from perpetual_os1.storage import StorageGrid
from perpetual_os1.common import RobotNatsConfiguration

class RobotClient:
    def __init__(self, config: RobotNatsConfiguration):
        self._config = config
        self._nc = NATS()
        self._js: Optional[JetStreamContext] = None
        self._status_adapter = TypeAdapter(WorkerStatus)
        self._storage_adapter = TypeAdapter(StorageGrid)

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._config.connection_string()])
        self._js = self._nc.jetstream()

    async def disconnect(self) -> None:
        await self._nc.drain()

    async def get_status(self) -> WorkerStatus:
        msg = await self._nc.request(self._config.get_status_subject, b"", timeout=2)
        return self._status_adapter.validate_json(msg.data)

    async def get_storage(self) -> StorageGrid:
        msg = await self._nc.request(self._config.get_storage_subject, b"", timeout=2)
        return self._storage_adapter.validate_json(msg.data)

    async def set_storage(self, storage: StorageGrid) -> None:
        await self._nc.request(
            self._config.set_storage_subject,
            storage.model_dump_json().encode(),
            timeout=2,
        )

    # async def get_configuration(self) -> RobotState:
    #     msg = await self._nc.request(self._config.get_config_subject, b"", timeout=2)
    #     return self._config_adapter.validate_json(msg.data)

    # async def set_configuration(self, config: RobotState) -> None:
    #     await self._nc.request(
    #         self._config.set_config_subject,
    #         config.model_dump_json().encode(),
    #         timeout=2,
    #     )

    async def send_command(self, command: RobotCommands) -> None:
        if self._js is None:
            raise RuntimeError("JetStream context not initialized")
        await self._js.publish(self._config.command_queue, command.model_dump_json().encode())
