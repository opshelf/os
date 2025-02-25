from nats.js import JetStreamContext
from pydantic import BaseModel
from perpetual_output_box.controller import OutputBox, OutputBoxConfiguration, OutputBoxStatus
from perpetual_output_box.events import (
    OutputBoxCommands,
    OutputBoxEvents,
    SetLockCommand,
    OpenDoorCommand,
    CloseDoorCommand,
    InitializeCommand,
    StopCommand,
    HomeCommand,
    ReceiveItemCommand,
)

from nats.aio.msg import Msg
import asyncio
from typing import Optional
from nats.aio.client import Client as NATS
from pydantic import BaseModel, TypeAdapter


class WorkerConfig(BaseModel):
    nats_url: str = "nats://localhost:4222"
    workstation_id: int
    output_box_id: int
    output_box_configuration: OutputBoxConfiguration


class WorkerStatus(BaseModel):
    output_box_status: Optional[OutputBoxStatus]
    running: bool
    current_command: Optional[OutputBoxCommands]


OutputBoxCommandAdapter = TypeAdapter[OutputBoxCommands](OutputBoxCommands)


class OutputBoxWorker:
    _instance: Optional["OutputBoxWorker"] = None

    def __init__(self, outputbox: OutputBox, config: WorkerConfig):
        if OutputBoxWorker._instance is not None:
            raise RuntimeError("OutputBoxWorker is a singleton. Use `get_instance()` to access it.")
        self._output_box = outputbox
        self._current_command: Optional[OutputBoxCommands] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self._running = False
        self._config = config
        self._nc = NATS()
        self._msg: Optional[Msg] = None
        #self._js: Optional[JetStreamContext] = None

    async def connect(self) -> None:
        """Connect to the NATS server."""
        await self._nc.connect(servers=[self._config.nats_url])
        self._js = self._nc.jetstream()
        await self._js.add_stream(
           name=f"workstation_{self._config.workstation_id}.output_box_{self._config.output_box_id}",
           subjects=[
               "commands",
               "events" 
           ],
        )
        self.work_consumer = await self._js.pull_subscribe(
           "commands", durable="commands"
        )

    @classmethod
    async def get_instance(
        cls, output_box: OutputBox, worker_config: WorkerConfig
    ) -> "OutputBoxWorker":
        if cls._instance is None:
            cls._instance = cls(output_box, worker_config)
        return cls._instance

    async def process_commands(self) -> None:
        try:
            while self._running:
                try:
                    if self._current_task is None:
                        msg = await self.work_consumer.fetch(timeout=1)
                        print(msg[0].data.decode())
                        cmd = OutputBoxCommandAdapter.validate_json(msg[0].data.decode())
                        print(cmd)
                    else:
                        if self._current_task and self._current_task.done():
                            match self._current_command:
                                # case StoreCommand(payload=payload):
                                #     await self._js.publish(
                                #         self._nats_config.event_queue,
                                #         StoringCompletedEvent(payload=payload.location)
                                #         .model_dump_json()
                                #         .encode(),
                                #     )
                                # case RetrieveCommand(payload=payload):
                                #    await self._js.publish(
                                #        self._nats_config.event_queue,
                                #        RetrievalCompletedEvent().model_dump_json().encode(),
                                #    )
                                case _:
                                    if self._current_command:
                                        await self._js.publish(
                                            self._nats_config.event_queue,
                                            self._current_task.result().model_dump_json().encode(),
                                        )

                                    else:
                                        print("No Current Command")
                                        print("Fell Through: ", self._current_command)
                                        print("Fell Through: ", self._current_task)
                                        print(self._current_task)
                            self._current_task = None
                            self._current_command = None
                            if self._msg:
                                await self._msg.ack()
                                self._msg = None
                    await asyncio.sleep(1)
                except Exception as e:
                    print(e)
        except Exception as e:
            print(e)

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
            self._worker_task = asyncio.create_task(self.process_commands())
            self._status = WorkerStatus(output_box_status=None, running=True, current_command=None)

    async def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._worker_task:
                self._worker_task.cancel()
                await self._worker_task
            await self._output_box.handle_command(StopCommand())
            print("Output Box shut down.")
            self._status = WorkerStatus(output_box_status=None, running=False, current_command=None)

    async def clear_commands(self) -> None:
        if self._js:
            await self._js.purge_stream(f"workstaton_{self._config.workstation_id}")

    # async def set_configuration(self, configuration: RobotConfiguration) -> None:
    #    await self._robot.set_configuration(configuration)

    # def get_configuration(self) -> RobotConfiguration:
    #    return self._robot.configuration
