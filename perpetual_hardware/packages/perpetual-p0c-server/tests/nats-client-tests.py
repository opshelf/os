import asyncio
import nats
import random
from nats.aio.client import Client as NATS
from nats.js.api import DeliverPolicy, StreamConfig
from nats.errors import TimeoutError
from typing import Optional
from nats.js import JetStreamContext
from perpetual_p0c.robot import RobotCommands, RobotResponses, StoreCommand, RetrieveCommand, ResponseStatus, StorageResponse, RetrieveResponse, StorePayload
from perpetual_p0c.storage import StorageModuleCellLocation, StorageModuleSide
from pydantic import parse_obj_as
import json

class Robot:

    async def handle_command(self, command: RobotCommands) -> RobotResponses: 
        match command:
            case StoreCommand(payload=payload):
                print(payload)
                await asyncio.sleep(random.random()*5)
                return StorageResponse(
                    command=command,
                    status=ResponseStatus.COMPLETED,
                )
            case RetrieveCommand(payload=payload):
                print(payload)
                await asyncio.sleep(random.random()*5)
                return RetrieveResponse(status=ResponseStatus.COMPLETED, command=command)
            #case PlaceTrayCommand(payload=payload):
            #    await self.place_tray(payload)
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="Tray Placed", command=command
            #    )
            #case PullTrayCommand(payload=payload):
            #    await self.pull_tray(payload)
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="Tray Pulled", command=command
            #    )
            #case HomeCommand():
            #    await self.home()
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="homed", command=command
            #    )
            #case HomeYFirstCommand():
            #    await self.home_y_first()
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="homed", command=command
            #    )
            #case StopCommand():
            #    await self.stop()
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="stopped", command=command
            #    )
            #case PauseCommand():
            #    raise NotImplementedError()
            #case InitializeCommand():
            #    await self.initialize()
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="initialized", command=command
            #    )
            #case ToolSubsystemCommand(payload=payload):
            #    await self.tool.handle_command(payload)
            #    return AckResponse(status=ResponseStatus.COMPLETED, message="tool", command=command)
            #case FunnelSubsystemCommand(payload=payload):
            #    await self.funnel.handle_command(payload)
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="funnel", command=command
            #    )
            #case WorkstationSubsystemCommand(payload=payload):
            #    await self.workstation.handle_command(payload)
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="workstation", command=command
            #    )
            #case GantrySubsystemCommands(payload=payload):
            #    await self.gantry.handle_command(payload)
            #    return AckResponse(
            #        status=ResponseStatus.COMPLETED, message="gantry", command=command
            #    )
            case _:
                raise Exception("Invalid Robot Command")



class RobotWorker:
    def __init__(self, nats_url, work_queue, response_queue, emergency_queue):
        self.nats_url = nats_url
        self.work_queue = work_queue
        self.response_queue = response_queue
        self.emergency_queue = emergency_queue
        self.robot = Robot()
        self.current_task: Optional[asyncio.Task] = None
        self.nc = NATS()
        self.processing = True  # Controls whether work_queue is being processed
        self.js: Optional[JetStreamContext] = None
        self.work_consumer: Optional[JetStreamContext.PullSubscription] = None

    async def connect(self):
        """Connect to the NATS server."""
        await self.nc.connect(servers=[self.nats_url])
        self.js = self.nc.jetstream()
        await self.js.add_stream(name="storage-module", subjects=["commands", "events"])
        streams = await self.js.streams_info()
        self.work_consumer = await self.js.pull_subscribe(
            "commands",
            durable="commands",
        )
        print("Connected to NATS")

    async def start(self):
        """Start the worker to listen on queues."""
        # Subscribe to work_queue
        if not self.work_consumer is None:
            print(f"Listening on {self.work_queue}...")
            while self.processing:
                try:
                    msg = await self.work_consumer.fetch(timeout=1)
                    if len(msg) > 0:
                        print(msg[0].data.decode())
                        cmd = parse_obj_as(RobotCommands, json.loads(msg[0].data.decode()))
                        print(cmd)
                        response = await self.robot.handle_command(cmd)
                        print(response)
                        await self.js.publish(
                            "events",
                            response.model_dump_json().encode()
                        )
                        await msg[0].ack()
                    await asyncio.sleep(.5)
                except TimeoutError:
                    pass
            print("Done Listening")

    async def close(self):
        """Close the NATS connection."""
        await self.nc.close()
        print("Disconnected from NATS")

    def stop(self):
        """Close the NATS connection."""
        self.processing = False
        print("Disconnected from NATS")


class RobotClient:
    def __init__(self, nats_url: str, subject: str, stream_name: str):
        self.nats_url = nats_url
        self.subject = subject
        self.stream_name = stream_name
        self.nc = NATS()

    async def connect(self):
        """Connect to the NATS server."""
        await self.nc.connect(servers=[self.nats_url])
        self.js = self.nc.jetstream()

        # Ensure the stream exists
        # await self.js.add_stream(StreamConfig(name=self.stream_name, subjects=[self.subject]))

    async def publish(self, cmd: RobotCommands):
        await self.js.publish(self.subject, cmd.model_dump_json().encode())

    async def get_all_messages(self):
        streams = await self.js.streams_info()
        for stream in streams:
            print(stream)
        consumer_info = await self.js.consumer_info(self.stream_name, self.subject)
        messages = []
        # Iterate over all available messages
        for seq in range(consumer_info.num_pending if consumer_info.num_pending else 0):
            msg = await self.js.get_msg(self.stream_name, seq + 1)
            print(msg)
            messages.append(msg)
        return messages

async def main():
    # Configuration
    nats_url = "nats://localhost:4222"
    work_queue = "storage-module"
    response_queue = "rpc_queue"
    emergency_queue = "emergency_queue"

    # Initialize Robot
    robot = RobotWorker(
        nats_url=nats_url,
        work_queue=work_queue,
        response_queue=response_queue,
        emergency_queue=emergency_queue,
    )
    client = RobotClient(
        nats_url="nats://localhost:4222", 
        subject="commands", 
        stream_name="storage-module"
    )
    await robot.connect()
    await client.connect()
    async def publisher():
        print("publisher started")
        for _ in range(0, 10):
            await client.publish(
                StoreCommand(
                    command="store_item",
                    payload=StorePayload(
                        location=StorageModuleCellLocation(
                            side=StorageModuleSide.RIGHT,
                            shelf_idx=0,
                            tray_idx=0,
                            slot_idx=0
                        ),
                        item=None
                    ))
                )
        await asyncio.sleep(10)
        await client.get_all_messages()
        await asyncio.sleep(5)
        robot.stop()
        await robot.close()

    await asyncio.gather(publisher(), robot.start())


# Run the main function
if __name__ == "__main__":
    asyncio.run(main())
