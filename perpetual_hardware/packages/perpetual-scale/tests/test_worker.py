import asyncio
from perpetual_scale import ScaleWorker
from perpetual_scale.ruishan import SerialScale  , get_scale_port

async def main():
    port = get_scale_port()
    print(port)
    scale = SerialScale(port.device, baudrate=9600)
    await scale.initialize()
    #scale = SerialScale("/dev/ttyACM0", baudrate=9600)
    print(await scale.read())
    nats_url = "nats://localhost:4222"
    scale_event_queue = "scale.events"

    worker = ScaleWorker(scale, nats_url, scale_event_queue)
    await worker.connect()
    await worker.start()

    try:
        # Just keep running
        while True:
            await asyncio.sleep(10)
    except KeyboardInterrupt:
        print("Stopping worker...")
    finally:
        await worker.stop()
        await worker.close()

if __name__ == "__main__":
    asyncio.run(main())
