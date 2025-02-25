from typing import Optional
from perpetual_waveshare.relay import RelayController
from perpetual_p0c_server.core.workstation.worker import WorkstationWorker, WorkstationClient
from perpetual_p0c_server.core.workstation.mock import MockWorkstation
from perpetual_p0c_server.core.workstation.waveshare import ModbusWorkstation
from perpetual_waveshare.utils import get_modbus_port
workstation_worker: Optional[WorkstationWorker] = None


async def init_workstation_worker() -> None:
    global workstation_worker
    port = get_modbus_port()
    if port:
        rc = RelayController(port, 57600, 1)
        await rc.connect()
        workstation = ModbusWorkstation(rc)
        workstation_worker = await WorkstationWorker.get_instance(workstation)
        await workstation_worker.connect()
        await workstation_worker.start()
    else:
        workstation = MockWorkstation()
        workstation_worker = await WorkstationWorker.get_instance(workstation)
        await workstation_worker.connect()
        await workstation_worker.start()


async def stop_workstation_worker() -> None:
    global workstation_worker
    if workstation_worker:
        await workstation_worker.shutdown()


async def get_workstation_client() -> WorkstationClient:
    if workstation_worker:
        return await workstation_worker.get_client()
    raise ValueError("Workstation Worker was Not Initialized")


async def get_workstation_worker() -> WorkstationWorker:
    if workstation_worker:
        return workstation_worker
    raise ValueError("Workstation Worker was Not Initialized")
