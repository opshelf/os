from perpetual_p0c.robot import HomeCommand, InitializeCommand, StopCommand
from perpetual_p0c_server.core.robot.nats import RobotWorker, RobotCommands
from perpetual_p0c_server.core.robot.mock import MockRobot
from perpetual_p0c.configuration import p0_robot_config
import asyncio


async def main() -> None:

    mock_robot = MockRobot(p0_robot_config)
    worker = RobotWorker(mock_robot)
    await worker.connect()
    client = await worker.get_client()

    for _ in range(0, 10):
        await client.send_command(HomeCommand())
        await client.send_command(StopCommand())
        await client.send_command(InitializeCommand())
    #commands = await client.get_queued_commands()
    #print(commands)


if __name__ == '__main__':
    asyncio.run(main())
