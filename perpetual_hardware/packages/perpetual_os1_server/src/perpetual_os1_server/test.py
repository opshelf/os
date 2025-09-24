import asyncio
from nats.aio.client import Client as NATS

async def main():
    nc = NATS()
    await nc.connect("nats://localhost:4222")
    print("✅ Connected to NATS")
    await nc.close()

asyncio.run(main())