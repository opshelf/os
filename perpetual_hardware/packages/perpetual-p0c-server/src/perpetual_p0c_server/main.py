from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from perpetual_p0c_server.core.printer.base import ThermalPrinter
from perpetual_p0c_server.core.storage import init_file_storage
from perpetual_p0c_server.core.sizes import init_file_sizes
from perpetual_p0c_server.core.robot import init_robot_worker, stop_robot_worker
from perpetual_p0c_server.core.scanner import init_scanner_worker, stop_scanner_worker
from perpetual_p0c_server.routers import storage, gantry, sizes, robot, scanner, printer
from contextlib import asynccontextmanager
from perpetual_p0c_server.core.printer import PrintWorker, MockPrinter
from typing import AsyncGenerator

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    print(app.title)
    #mock_printer = MockPrinter()
    tp_printer = ThermalPrinter()
    print_worker = PrintWorker(
        tp_printer, "nats://localhost:4222", "printer_queue", "printer_event_queue"
    )
    await print_worker.connect()
    init_file_storage()
    init_file_sizes()
    await init_scanner_worker(False)
    await init_robot_worker(False)
    yield
    await stop_robot_worker()
    await stop_scanner_worker()
    await print_worker.close()


app = FastAPI(lifespan=lifespan)

origins = ["http://localhost", "http://localhost:8080", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(storage.router, prefix="/api/storage")
app.include_router(sizes.router, prefix="/api/sizes")
app.include_router(gantry.router, prefix="/api/gantry")
app.include_router(robot.router, prefix="/api/robot")
app.include_router(scanner.router, prefix="/api/scanner")
app.include_router(printer.router, prefix="/api/printer")



# def save_schemas_to_files():
#    file_path = "./app/routers/types.py"
#    models = get_base_models(file_path)
#    for model in models:
#        print(model.model_json_schema)
#
# save_schemas_to_files()
# schemas = load_models_and_get_json_schema(Path("./app/routers/types.py"))
# with open("./schemas.json", "w") as f:
#    json.dump(schemas, f, indent=4)
async def main() -> None:
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_graceful_shutdown=2,  # Graceful shutdown timeout in seconds
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    import uvicorn
    import asyncio
    asyncio.run(main())
