from perpetual_p0c.robot import P0C, RobotConfiguration, RobotCommands

# from abc import ABC, abstractmethod
from perpetual_can import CANBusHandler
import asyncio
from nats.aio.client import Client as NATS

# from nats.aio.msg import Msg
# from asyncio.queues import Queue
from typing import Optional


class RobotWorker:
    def __init__(
        self,
        bus: CANBusHandler,
        configuration: RobotConfiguration,
    ) -> None:
        self._robot = P0C(bus, configuration)
        self._bus = bus
        self._nc = NATS()
        self._work_queue_name = "commands"
        self._response_queue_name = "events"
        self._emergency_queue_name = "emergency"
        self._nats_url = "nats://localhost:4222"
        self._current_task: Optional[asyncio.Task[None]] = None

    async def handle_command(self, command: RobotCommands) -> None:
        await self._robot.handle_command(command)

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._nats_url])

    # async def start(self) -> None:
    #    # Subscribe to work_queue
    #    await self._nc.subscribe(self._work_queue, cb=self.process_message)
    #    # Subscribe to emergency_queue
    #    await self._nc.subscribe(self._emergency_queue, cb=self.handle_emergency)


"""
class RobotWorker:
    def __init__(self, nats_url: str, work_queue: str, response_queue: str, emergency_queue: str) -> None:
        self.nats_url = nats_url
        self.work_queue = work_queue
        self.response_queue = response_queue
        self.emergency_queue = emergency_queue
        self.nc = NATS()
        self.processing = True  # Controls whether work_queue is being processed

    async def connect(self) -> None:
        await self.nc.connect(servers=[self.nats_url])
        print("Connected to NATS")

    async def start(self) -> None:
        # Subscribe to work_queue
        await self.nc.subscribe(self.work_queue, cb=self.process_message)
        print(f"Listening on {self.work_queue}...")

        # Subscribe to emergency_queue
        await self.nc.subscribe(self.emergency_queue, cb=self.handle_emergency)
        print(f"Listening on {self.emergency_queue} for stop/resume commands...")

    async def process_message(self, msg: Msg) -> None:
        if not self.processing:
            print("Paused. Ignoring messages.")
            return

        data = msg.data.decode()
        print(data)
        response = f"Processed: {data}"
        # Publish response to response_queue
        await self.nc.publish(self.response_queue, response.encode())

    async def handle_emergency(self, msg: Msg) -> None:
        command = msg.data.decode().lower()
        if command == "stop":
            self.processing = False
            print("Received STOP command. Pausing message processing.")
        elif command == "resume":
            self.processing = True
            print("Received RESUME command. Resuming message processing.")
        else:
            print(f"Unknown command on {self.emergency_queue}: {command}")

    async def close(self) -> None:
        await self.nc.close()
        print("Disconnected from NATS")


async def publish_messages(nc: NATS, work_queue: str, emergency_queue: str) -> None:
    count = 0
    while True:
        # Publish a random message to the work_queue
        msg = RobotMessage(command=RobotCommand(
            command="HOME",
        ))
        await nc.publish(work_queue, msg.model_dump_json().encode())
        msg = RobotMessage(command=RobotCommand(
            command="STOP",
        ))
        await nc.publish(work_queue, msg.model_dump_json().encode())
        count += 1

        # Randomly send stop or resume commands to the emergency_queue
        if random.random() < 0.2:  # 20% chance of sending a command
            command = random.choice(["stop", "resume"])
            await nc.publish(emergency_queue, command.encode())
        await asyncio.sleep(2)  # Delay between messages


async def main() -> None:
    # Configuration
    nats_url = "nats://localhost:4222"
    work_queue = "work_queue"
    response_queue = "response_queue"
    emergency_queue = "emergency_queue"

    # Initialize Robot
    robot = RobotWorker(
        nats_url=nats_url,
        work_queue=work_queue,
        response_queue=response_queue,
        emergency_queue=emergency_queue,
    )

    # Connect to NATS
    await robot.connect()

    # Start the worker and publisher concurrently
    await asyncio.gather(
        robot.start(),
        publish_messages(robot.nc, work_queue, emergency_queue),
    )

    # Close connection on exit
    await robot.close()

# Run the main function
if __name__ == "__main__":
    asyncio.run(main())

"""
