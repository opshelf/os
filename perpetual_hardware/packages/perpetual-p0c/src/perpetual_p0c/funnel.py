import asyncio
from pydantic import BaseModel, Field
from typing import Literal, Union, Annotated
from perpetual_p0c.storage import Tray
from perpetual_p0c.io_board import IoController, Wedge
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_trinamic.aio.trinamic_stepper import TrinamicStepper
from perpetual_can import CANBusHandler
from perpetual_p0c.storage import Tray
from perpetual_p0c.common import AbstractSubsystem, CanAxisConfiguration
from pydantic import BaseModel
from enum import Enum


class FunnelOffsets(BaseModel):
    funnel_min_width: float
    funnel_min_height: float
    funnel_cell_1_offset_x: float
    funnel_cell_1_offset_y: float
    catch_position: float
    diverter_50mm_pos: float
    diverter_100mm_pos: float
    diverter_150mm_pos: float
    belt_speed: float = 150
    wall_speed: float = 500
    output_transfer_conveyor_speed: float = 100
    output_conveyor_speed: float = 250
    slot_gap_size: float = 10.0


class FunnelConfiguration(BaseModel):
    inlet_funnel: CanAxisConfiguration
    opposing_funnel: CanAxisConfiguration
    slot_1: CanAxisConfiguration
    slot_2: CanAxisConfiguration
    slot_3: CanAxisConfiguration
    belt: CanAxisConfiguration
    wall: CanAxisConfiguration
    output_transfer_conveyor: CanAxisConfiguration
    output_conveyor: CanAxisConfiguration
    diverter: CanAxisConfiguration
    relay_board_id: int
    offsets: FunnelOffsets
    # opposing_funnel_max_travel: float = 362
    opposing_funnel_max_travel: float = 340
    input_funnel_max_travel: float = 370.0
    minimum_transfer_location: float = 325.0


class FunnelStatus(BaseModel):
    inlet_funnel: float
    opposing_funnel: float
    slot_1: float
    slot_2: float
    slot_3: float


class FunnelOpeningSize(int, Enum):
    OPEN_50MM = 0
    OPEN_100MM = 1
    OPEN_150MM = 2


class ShutdownCommand(BaseModel):
    command: Literal["shutdown"]


class StopCommand(BaseModel):
    command: Literal["stop"]


class InitializeCommand(BaseModel):
    command: Literal["initialize"]


class HomeCommand(BaseModel):
    command: Literal["home"]


class StopBeltCommand(BaseModel):
    command: Literal["stop_belt"]


class StartBeltCommand(BaseModel):
    command: Literal["start_belt"]


class StopWallCommand(BaseModel):
    command: Literal["stop_wall"]


class StartWallCommand(BaseModel):
    command: Literal["start_wall"]


class StopOutputCommand(BaseModel):
    command: Literal["stop_output"]


class StartOutputCommand(BaseModel):
    command: Literal["start_output"]


class StopOutputTransferCommand(BaseModel):
    command: Literal["stop_output_transfer"]


class StartOutputTransferCommand(BaseModel):
    command: Literal["start_output_transfer"]


class MoveToCellCommand(BaseModel):
    command: Literal["move_to_cell"]
    tray: Tray
    idx: int


FunnelCommands = Annotated[
    Union[
        ShutdownCommand,
        StopCommand,
        InitializeCommand,
        HomeCommand,
        StopBeltCommand,
        StartBeltCommand,
        StopWallCommand,
        StartWallCommand,
        StopOutputCommand,
        StartOutputCommand,
        StopOutputTransferCommand,
        StartOutputTransferCommand,
        MoveToCellCommand,
    ],
    Field(discriminator="command"),
]


class Funnel(AbstractSubsystem[FunnelConfiguration, FunnelStatus]):
    def __init__(self, bus: CANBusHandler, funnel_configuration: FunnelConfiguration):
        self._configuration = funnel_configuration
        self._inlet_funnel = TrinamicAxis(
            bus,
            configuration=funnel_configuration.inlet_funnel.configuration,
            module_id=funnel_configuration.inlet_funnel.can_id,
            motor_id=0,
        )
        self._opposing_funnel = TrinamicAxis(
            bus,
            configuration=funnel_configuration.opposing_funnel.configuration,
            module_id=funnel_configuration.opposing_funnel.can_id,
            motor_id=0,
        )
        self._slot_1 = TrinamicAxis(
            bus,
            configuration=funnel_configuration.slot_1.configuration,
            module_id=funnel_configuration.slot_1.can_id,
            motor_id=0,
        )
        self._slot_2 = TrinamicAxis(
            bus,
            configuration=funnel_configuration.slot_2.configuration,
            module_id=funnel_configuration.slot_2.can_id,
            motor_id=0,
        )
        self._slot_3 = TrinamicAxis(
            bus,
            configuration=funnel_configuration.slot_3.configuration,
            module_id=funnel_configuration.slot_3.can_id,
            motor_id=0,
        )
        self._io = IoController(bus)
        self._belt = TrinamicStepper(
            bus,
            configuration=funnel_configuration.belt.configuration,
            module_id=funnel_configuration.belt.can_id,
            motor_id=0,
            max_current=6000,
        )
        self._wall = TrinamicStepper(
            bus,
            configuration=funnel_configuration.wall.configuration,
            module_id=funnel_configuration.wall.can_id,
            motor_id=0,
        )
        self._diverter = TrinamicStepper(
            bus,
            configuration=funnel_configuration.diverter.configuration,
            module_id=funnel_configuration.diverter.can_id,
            motor_id=0,
        )
        self._output_transfer_conveyor = TrinamicStepper(
            bus,
            configuration=funnel_configuration.output_transfer_conveyor.configuration,
            module_id=funnel_configuration.output_transfer_conveyor.can_id,
            motor_id=0,
            max_current=6000,
        )
        self._output_conveyor = TrinamicStepper(
            bus,
            configuration=funnel_configuration.output_conveyor.configuration,
            module_id=funnel_configuration.output_conveyor.can_id,
            motor_id=0,
            max_current=6000,
        )
        self._slots_open = 0
        self._diverter_open = True

    async def cleanup(self) -> None:
        await self._inlet_funnel.stop()
        await self._opposing_funnel.stop()
        await self._slot_1.stop()
        await self._slot_2.stop()
        await self._slot_3.stop()
        await self._belt.stop()
        await self._wall.stop()
        await self._diverter.stop()
        await self._diverter.set_standby_current(0)
        await self._output_transfer_conveyor.stop()
        await self._output_conveyor.stop()
        await self._belt.stop()
        await self._wall.stop()
        await self._output_transfer_conveyor.stop()

    @property
    def belt(self) -> TrinamicStepper:
        return self._belt

    @property
    def wall(self) -> TrinamicStepper:
        return self._wall

    @property
    def output_transfer_conveyor(self) -> TrinamicStepper:
        return self._output_transfer_conveyor

    @property
    async def output_conveyor(self) -> TrinamicStepper:
        return self._output_conveyor

    async def output_conveyor_stop(self) -> None:
        await self._output_transfer_conveyor.stop()

    async def stop_belt(self) -> None:
        await self._belt.stop()

    async def start_belt(self) -> None:
        await self._belt.initialize()
        await self._belt.rotate(int(self._configuration.offsets.belt_speed))

    async def stop_wall(self) -> None:
        await self._wall.stop()

    async def start_wall(self) -> None:
        await self._wall.initialize()
        await self._wall.rotate(int(self._configuration.offsets.wall_speed))

    async def stop_output(self) -> None:
        await self._output_conveyor.stop()

    async def start_output(self) -> None:
        await self._output_conveyor.initialize()
        await self._output_conveyor.rotate(int(self._configuration.offsets.output_conveyor_speed))

    async def stop_output_transfer(self) -> None:
        await self._output_transfer_conveyor.stop()

    async def start_output_transfer(self) -> None:
        await self._output_transfer_conveyor.initialize()
        await self._output_transfer_conveyor.rotate(
            int(self._configuration.offsets.output_transfer_conveyor_speed)
        )
    
    @property
    async def configuration(self) -> FunnelConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: FunnelConfiguration) -> None:
        self._configuration = configuration
        self._inlet_funnel.configuration = self._configuration.inlet_funnel.configuration
        self._opposing_funnel.configuration = self._configuration.opposing_funnel.configuration
        self._slot_1.configuration = self._configuration.slot_1.configuration
        self._slot_2.configuration = self._configuration.slot_2.configuration
        self._slot_3.configuration = self._configuration.slot_3.configuration
        self._belt.configuration = self._configuration.belt.configuration
        self._wall.configuration = self._configuration.wall.configuration
        await self.initialize()

    @property
    def offsets(self) -> FunnelOffsets:
        return self._configuration.offsets

    @offsets.setter
    def offsets(self, offsets: FunnelOffsets) -> None:
        self._configuration.offsets = offsets

    async def start_receive_item(self) -> None:
        await self.start_belt()
        await self.start_wall()

    async def stop_receive_item(self) -> None:
        await self.stop_belt()
        await self.stop_wall()

    @property
    def io(self) -> IoController:
        return self._io

    @property
    def slots_open(self) -> int:
        return self._slots_open

    async def open_slots(self, slots: int) -> None:
        self._slots_open = slots
        await self._io.set_wedges(
            Wedge(
                wedge_3=False if slots > 2 else True,
                wedge_2=False if slots > 1 else True,
                wedge_1=False if slots > 0 else True,
            )
        )

    async def open_tray_wedges(self, tray: Tray) -> None:
        if tray.cell_width <= 50:
            await self._io.set_wedges(
                Wedge(
                    wedge_1=False,
                    wedge_2=True,
                    wedge_3=True,
                )
            )
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            await self._io.set_wedges(
                Wedge(
                    wedge_1=False,
                    wedge_2=False,
                    wedge_3=True,
                )
            )
        else:
            await self._io.set_wedges(
                Wedge(
                    wedge_1=False,
                    wedge_2=False,
                    wedge_3=False,
                )
            )

    async def close_all_wedges(self) -> None:
        self._slots_open = 0
        await self._io.set_wedges(Wedge(wedge_1=True, wedge_2=True, wedge_3=True))

    async def open_all_wedges(self) -> None:
        await self._io.set_wedges(Wedge())

    @property
    def inlet_funnel(self) -> TrinamicAxis:
        return self._inlet_funnel

    @property
    def opposing_funnel(self) -> TrinamicAxis:
        return self._opposing_funnel

    @property
    def slot_1(self) -> TrinamicAxis:
        return self._slot_1

    @property
    def slot_2(self) -> TrinamicAxis:
        return self._slot_2

    @property
    def slot_3(self) -> TrinamicAxis:
        return self._slot_3

    @property
    def diverter(self) -> TrinamicStepper:
        return self._diverter

    async def return_slots(self) -> None:
        await self._slot_1.move_to_position(0)
        await self._slot_2.move_to_position(0)
        await self._slot_3.move_to_position(0)
        await self.wait_for_move()

    async def home(self) -> None:
        await self._inlet_funnel.home()
        await self._opposing_funnel.home()
        await self._slot_1.home()
        await self._slot_2.home()
        await self._slot_3.home()
        await self._diverter.home()

    async def check_position_reached(self) -> bool:
        return all(
            [
                await self._inlet_funnel.poll_position_reached(),
                await self._opposing_funnel.poll_position_reached(),
                await self._slot_1.poll_position_reached(),
                await self._slot_2.poll_position_reached(),
                await self._slot_3.poll_position_reached(),
            ]
        )

    async def wait_for_move(self) -> None:
        await self._inlet_funnel.wait_for_move()
        await self._opposing_funnel.wait_for_move()
        await self._slot_1.wait_for_move()
        await self._slot_2.wait_for_move()
        await self._slot_3.wait_for_move()
        await self._diverter.wait_for_move()

    async def open_tray_slots(self, tray: Tray) -> None:
        if tray.cell_width <= 50:
            await self._slot_1.move_to_position(self._configuration.offsets.slot_gap_size * 2)
            await self._slot_2.move_to_position(
                tray.cell_width - self._configuration.offsets.slot_gap_size
            )
            await self._slot_3.move_to_position(
                tray.cell_width - self._configuration.offsets.slot_gap_size
            )
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            await self._slot_1.move_to_position(self._configuration.offsets.slot_gap_size * 2)
            await self._slot_2.move_to_position(self._configuration.offsets.slot_gap_size * 2)
            await self._slot_3.move_to_position(
                tray.cell_width - self._configuration.offsets.slot_gap_size
            )
        else:
            await self._slot_1.move_to_position(self._configuration.offsets.slot_gap_size * 2)
            await self._slot_2.move_to_position(self._configuration.offsets.slot_gap_size * 2)
            await self._slot_3.move_to_position(self._configuration.offsets.slot_gap_size * 2)

    async def close_tray_wedges(self, tray: Tray, times: int = 1) -> None:
        for _ in range(0, times):
            await self.close_all_wedges()
            await asyncio.sleep(1)
            await self.open_tray_wedges(tray)

    async def set_diverter_opening(self, tray: Tray) -> None:
        await self._diverter.initialize()
        if tray.cell_width <= 50.0:
            await self._diverter.move_revolutions(self._configuration.offsets.diverter_50mm_pos)
        elif tray.cell_width > 50.0 and tray.cell_width <= 105.0:
            await self._diverter.move_revolutions(self._configuration.offsets.diverter_100mm_pos)
        else:
            await self._diverter.move_revolutions(self._configuration.offsets.diverter_150mm_pos)

    async def move_to_cell(self, tray: Tray, idx: int) -> None:
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        ipos, opos = tray.get_cell_position(idx)
        await self.open_tray_slots(tray)
        await self.open_tray_wedges(tray)
        await self.wait_for_move()
        ipos += self.offsets.funnel_cell_1_offset_x
        opos = self._configuration.opposing_funnel_max_travel - opos
        opos -= self.offsets.funnel_cell_1_offset_x
        opos -= self.offsets.funnel_min_width
        opos = min([self._configuration.opposing_funnel_max_travel, opos])
        ipos = min([self._configuration.input_funnel_max_travel, ipos])
        opos = max([0, opos])
        curr_opos = await self._opposing_funnel.get_position()
        if opos >= curr_opos:
            print(f"Move To Cell Opposing: {opos} Input: {ipos} Great Opos")
            await self._inlet_funnel.move_to_position(ipos)
            await self._opposing_funnel.move_to_position(opos)
            await self.wait_for_move()
        else:
            print(f"Move To Cell Opposing: {opos} Input: {ipos} Less Opos")
            await self._opposing_funnel.move_to_position(opos)
            await self._inlet_funnel.move_to_position(ipos)
            await self.wait_for_move()
        await self.open_tray_slots(tray)
        await self.wait_for_move()

    async def return_diverter(self) -> None:
        await self._diverter.initialize()
        await self._diverter.move_revolutions(0.0)
        await self.wait_for_move()

    async def move_to_cell_catch(self, tray: Tray, idx: int) -> None:
        await self.inlet_funnel.move_to_position(0)
        await self.opposing_funnel.move_to_position(0)
        await self.return_slots()
        await self.wait_for_move()
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        ipos, opos = tray.get_cell_position(idx)
        await self.open_tray_slots(tray)
        await self.open_tray_wedges(tray)
        ipos += self.offsets.funnel_cell_1_offset_x
        opos = self._configuration.opposing_funnel_max_travel - opos
        opos -= self.offsets.funnel_cell_1_offset_x
        opos += self.offsets.funnel_min_width
        opos = min([self._configuration.opposing_funnel_max_travel, opos])
        ipos = min([self._configuration.opposing_funnel_max_travel, ipos])
        opos = max([0, opos])
        curr_opos = await self._opposing_funnel.get_position()
        await self.set_diverter_opening(tray)
        if opos >= curr_opos:
            print(f"Move To Cell Opposing: {opos} Input: {ipos} Greater Opos")
            await self._opposing_funnel.move_to_position(opos)
            await self._inlet_funnel.move_to_position(ipos)
        else:
            print(f"Move To Cell Opposing: {opos} Input: {ipos} Less Opos")
            await self._inlet_funnel.move_to_position(ipos)
            await self._opposing_funnel.move_to_position(opos)
        await self.wait_for_move()

    async def return_tray_slots(self, tray: Tray, idx: int) -> None:
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        ipos, opos = tray.get_cell_position(idx)
        diff = opos - ipos
        slot_pos = diff - self._configuration.offsets.slot_gap_size
        if tray.cell_width <= 50:
            await self._slot_1.move_to_position(slot_pos)
            await self._slot_2.move_to_position(slot_pos)
            await self._slot_3.move_to_position(0)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            await self._slot_1.move_to_position(slot_pos)
            await self._slot_2.move_to_position(0)
            await self._slot_3.move_to_position(0)
        else:
            await self._slot_1.move_to_position(0)
            await self._slot_2.move_to_position(0)
            await self._slot_3.move_to_position(0)

    async def status(self) -> FunnelStatus:
        ipos = await self._inlet_funnel.get_position()
        opos = await self._opposing_funnel.get_position()
        slot_1 = await self._slot_1.get_position()
        slot_2 = await self._slot_2.get_position()
        slot_3 = await self._slot_3.get_position()

        slot_1 = 0
        slot_2 = 0
        slot_3 = 0
        return FunnelStatus(
            inlet_funnel=ipos,
            opposing_funnel=opos,
            slot_1=slot_1,
            slot_2=slot_2,
            slot_3=slot_3,
        )

    async def stop(self) -> None:
        await self._inlet_funnel.stop()
        await self._opposing_funnel.stop()
        await self._slot_1.stop()
        await self._slot_2.stop()
        await self._slot_3.stop()
        await self._belt.stop()
        await self._wall.stop()
        await self._output_transfer_conveyor.stop_motor()
        await self._output_conveyor.stop_motor()

    async def initialize(self) -> None:
        await self._inlet_funnel.initialize()
        await self._opposing_funnel.initialize()
        await self._slot_1.initialize()
        await self._slot_2.initialize()
        await self._slot_3.initialize()
        await self._belt.initialize()
        await self._wall.initialize()

    async def catch(self, tray: Tray, idx: int) -> None:
        max_travel = self._configuration.opposing_funnel_max_travel
        min_travel = 250
        conveyor_width = 205
        # min_transfer_location = 220
        start, _ = tray.get_cell_position(idx)
        position = max_travel - start + conveyor_width / 2
        position = max(position, min_travel)
        position = min(position, max_travel)

        await self.return_slots()
        await self.inlet_funnel.move_to_position(0)
        await self.inlet_funnel.wait_for_move()
        await self.opposing_funnel.move_to_position(position)

    async def move_to_transfer_location(self) -> None:
        opos = await self.opposing_funnel.get_position()
        if opos < self._configuration.minimum_transfer_location:
            await self.opposing_funnel.move_to_position(
                self._configuration.minimum_transfer_location
            )
            await self.opposing_funnel.wait_for_move()

    async def setup_receive_item(self, tray: Tray, idx: int) -> None:
        await self.return_diverter()
        await self.inlet_funnel.move_to_position(0)
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        _, opos = tray.get_cell_position(idx)
        await self.return_slots()
        #await self.open_tray_slots(tray)
        await self.open_tray_wedges(tray)
        await self.wait_for_move()
        opos = self._configuration.opposing_funnel_max_travel - opos
        opos -= self.offsets.funnel_cell_1_offset_x
        opos -= self.offsets.funnel_min_width
        opos = min([self._configuration.opposing_funnel_max_travel, opos])
        opos = max([0, opos])
        print("Setup Receive Item Opposing Funnel Position: ", opos)
        await self._opposing_funnel.move_to_position(opos)
        await self.wait_for_move()


        #position = tray.cell_width - self.offsets.funnel_min_height
        #if position >= 150:
        #    position = 148
        #ipos, opos = tray.get_cell_position(idx)
        #await self.open_tray_slots(tray)
        #await self.open_tray_wedges(tray)
        #await self.wait_for_move()
        #ipos += self.offsets.funnel_cell_1_offset_x
        #opos = self._configuration.opposing_funnel_max_travel - opos
        #opos -= self.offsets.funnel_cell_1_offset_x
        #opos -= self.offsets.funnel_min_width
        #opos = min([self._configuration.opposing_funnel_max_travel, opos])
        #ipos = min([self._configuration.input_funnel_max_travel, ipos])
        #opos = max([0, opos])
        #curr_opos = await self._opposing_funnel.get_position()

    async def handle_command(self, command: FunnelCommands) -> None:
        match command:
            case ShutdownCommand():
                await self.cleanup()
            case StopCommand():
                await self.stop()
            case InitializeCommand():
                await self.initialize()
            case HomeCommand():
                await self.home()
            case StopBeltCommand():
                await self.stop_belt()
            case StartBeltCommand():
                await self.start_belt()
            case StopWallCommand():
                await self.stop_wall()
            case StartWallCommand():
                await self.start_wall()
            case StopOutputCommand():
                await self.stop_output()
            case StartOutputCommand():
                await self.start_output()
            case StopOutputTransferCommand():
                await self.stop_output_transfer()
            case StartOutputTransferCommand():
                await self.start_output_transfer()
            case MoveToCellCommand(tray=tray, idx=idx):
                await self.move_to_cell(tray, idx)
            case _:
                raise Exception("Invalid Command Exception")
