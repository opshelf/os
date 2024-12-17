import asyncio
import random
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

class Robot:
    def __init__(self, nats_url: str, work_queue: str, response_queue: str, emergency_queue: str) -> None:
        self.nats_url = nats_url
        self.work_queue = work_queue
        self.response_queue = response_queue
        self.emergency_queue = emergency_queue
        self.nc = NATS()
        self.processing = True  # Controls whether work_queue is being processed

    async def connect(self) -> None:
        """Connect to the NATS server."""
        await self.nc.connect(servers=[self.nats_url])
        print("Connected to NATS")

    async def start(self) -> None:
        """Start the worker to listen on queues."""
        # Subscribe to work_queue
        await self.nc.subscribe(self.work_queue, cb=self.process_message)
        print(f"Listening on {self.work_queue}...")

        # Subscribe to emergency_queue
        await self.nc.subscribe(self.emergency_queue, cb=self.handle_emergency)
        print(f"Listening on {self.emergency_queue} for stop/resume commands...")

    async def process_message(self, msg: Msg) -> None:
        """Process incoming messages from work_queue."""
        if not self.processing:
            print("Paused. Ignoring messages.")
            return

        data = msg.data.decode()
        print(f"Received message on {self.work_queue}: {data}")

        # Simulate processing
        response = f"Processed: {data}"
        await asyncio.sleep(1)

        # Publish response to response_queue
        await self.nc.publish(self.response_queue, response.encode())
        print(f"Published response to {self.response_queue}: {response}")

    async def handle_emergency(self, msg: Msg) -> None:
        """Handle stop/resume commands from emergency_queue."""
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
        """Close the NATS connection."""
        await self.nc.close()
        print("Disconnected from NATS")


async def publish_messages(nc: NATS, work_queue: str, emergency_queue: str) -> None:
    """Simulate publishing messages independently."""
    count = 0
    while True:
        # Publish a random message to the work_queue
        message = f"Task {count}"
        await nc.publish(work_queue, message.encode())
        print(f"Published to {work_queue}: {message}")
        count += 1

        # Randomly send stop or resume commands to the emergency_queue
        if random.random() < 0.2:  # 20% chance of sending a command
            command = random.choice(["stop", "resume"])
            await nc.publish(emergency_queue, command.encode())
            print(f"Published to {emergency_queue}: {command}")

        await asyncio.sleep(2)  # Delay between messages


async def main() -> None:
    # Configuration
    nats_url = "nats://localhost:4222"
    work_queue = "work_queue"
    response_queue = "response_queue"
    emergency_queue = "emergency_queue"

    # Initialize Robot
    robot = Robot(
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
