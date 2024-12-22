import asyncio
from perpetual_p0c.storage import Tray
from perpetual_p0c.aio.io_board import IoController, Wedge
from perpetual_p0c.funnel import FunnelStatus, FunnelOffsets, FunnelCommands, FunnelOpeningSize, FunnelConfiguration
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_trinamic.aio.trinamic_stepper import TrinamicStepper
import time
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_can import CANBusHandler

class Funnel:
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

    #async def __enter__(self) -> "Funnel":
    #   return self

    #async def __exit__(self) -> None:
    #   await self.cleanup()

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

    async def output_transfer_conveyor_rotate(self, speed: int = -600) -> None:
        await self._output_transfer_conveyor.rotate(speed)

    async def output_transfer_conveyor_stop(self) -> None:
        await self._output_transfer_conveyor.stop()

    @property
    async def output_conveyor(self) -> TrinamicStepper:
        return self._output_conveyor

    async def output_conveyor_rotate(self, speed: int = -600) -> None:
        await self._output_conveyor.rotate(speed)

    async def output_conveyor_stop(self) -> None:
        await self._output_transfer_conveyor.stop()

    async def stop_belt(self) -> None:
        await self._belt.stop()

    async def start_belt(self) -> None:
        await self._belt.initialize()
        await self._belt.safety_off()
        await self._belt.rotate(int(self._configuration.offsets.belt_speed))

    async def stop_wall(self) -> None:
        await self._wall.stop()

    async def start_wall(self) -> None:
        await self._wall.initialize()
        await self._wall.safety_off()
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
        await self._output_transfer_conveyor.safety_off()
        await self._output_transfer_conveyor.rotate(
            int(self._configuration.offsets.output_transfer_conveyor_speed)
        )

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

    async def close_slots(self) -> None:
        self._slots_open = 0
        # self._slots.set_relays(GenericRelayBoardMessage(r2=True, r3=True, r4=True))
        await self._io.set_wedges(Wedge(wedge_1=True, wedge_2=True, wedge_3=True))

    async def open_all_slots(self) -> None:
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
        #await self._slot_1.home()
        #await self._slot_2.home()
        #await self._slot_3.home()

        await self._slot_1.initialize()
        await self._slot_2.initialize()
        await self._slot_3.initialize()

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
        #await self._slot_1.wait_for_move()
        #await self._slot_2.wait_for_move()
        #await self._slot_3.wait_for_move()

    async def open_tray_slots(self, tray: Tray) -> None:
        if tray.cell_width <= 50:
            await self._slot_1.move_to_position(0)
            await self._slot_2.move_to_position(tray.cell_width - 5)
            await self._slot_3.move_to_position(tray.cell_width - 5)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            await self._slot_1.move_to_position(0)
            await self._slot_2.move_to_position(0)
            await self._slot_3.move_to_position(tray.cell_width - 5)
        else:
            await self._slot_1.move_to_position(0)
            await self._slot_2.move_to_position(0)
            await self._slot_3.move_to_position(0)

    async def close_tray_wedges(self, tray: Tray, times: int = 1) -> None:
        for _ in range(0, times):
            await self.close_slots()
            time.sleep(1)
            await self.open_tray_wedges(tray)

    async def set_diverter_opening(self, tray: Tray) -> None:
        if tray.cell_width <= 50:
            await self.set_diverter_size(FunnelOpeningSize.OPEN_50MM)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            await self.set_diverter_size(FunnelOpeningSize.OPEN_100MM)
        else:
            await self.set_diverter_size(FunnelOpeningSize.OPEN_150MM)

    async def move_to_cell(self, tray: Tray, idx: int) -> None:
        # self.inlet_funnel.move_to_position(0)
        # self.opposing_funnel.move_to_position(0)
        # await self.return_slots()
        # self.wait_for_move()
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        ipos, opos = tray.get_cell_position(idx)
        await self.open_tray_slots(tray)
        await self.open_tray_wedges(tray)
        ipos += self.offsets.funnel_cell_1_offset_x
        opos = self._configuration.opposing_funnel_max_travel - opos
        opos -= self.offsets.funnel_cell_1_offset_x
        opos -= self.offsets.funnel_min_width
        opos = min([self._configuration.opposing_funnel_max_travel, opos])
        ipos = min([self._configuration.input_funnel_max_travel, ipos])
        opos = max([0, opos])
        curr_opos = await self._opposing_funnel.get_position()
        if opos >= curr_opos:
            await self._inlet_funnel.move_to_position(ipos)
            await self._opposing_funnel.move_to_position(opos)
            await self.wait_for_move()
        else:
            await self._opposing_funnel.move_to_position(opos)
            await self._inlet_funnel.move_to_position(ipos)
            await self.wait_for_move()

    async def set_diverter_size(self, size: FunnelOpeningSize) -> None:
        if size == FunnelOpeningSize.OPEN_50MM:
            await self._diverter.move_revolutions(self._configuration.offsets.diverter_50mm_pos)
        elif size == FunnelOpeningSize.OPEN_100MM:
            await self._diverter.move_revolutions(self._configuration.offsets.diverter_100mm_pos)
        elif size == FunnelOpeningSize.OPEN_150MM:
            await self._diverter.move_revolutions(self._configuration.offsets.diverter_150mm_pos)
        else:
            raise Exception("Invalid Opening")
        await self.wait_for_move()

    async def return_diverter(self) -> None:
        await self._diverter.move_revolutions(0.0)
        await self.wait_for_move()

    async def wiggle(self, revolutions: float = 0.2) -> None:
        curr_pos = await self._diverter.get_position()
        try:
            await self._diverter.move_revolutions(0)
            await self._diverter.wait_for_move()
            await asyncio.sleep(0.25)
            await self._diverter.move_revolutions(curr_pos)
            await self._diverter.wait_for_move()
            await self._diverter.move_relative_revolutions(revolutions)
            await self._diverter.wait_for_move()
            await self._diverter.move_relative_revolutions(-revolutions)
            await self._diverter.wait_for_move()
            await self._diverter.move_relative_revolutions(revolutions)
            await self._diverter.wait_for_move()
            await self._diverter.move_relative_revolutions(-revolutions)
            await self._diverter.wait_for_move()
        except Exception as e:
            print(e)
            await self._diverter.initialize()
            await self._diverter.move_revolutions(curr_pos)
            await self._diverter.wait_for_move()

    async def move_to_cell_catch(self, tray: Tray, idx: int) -> None:
        await self.inlet_funnel.move_to_position(0)
        await self.opposing_funnel.move_to_position(0)
        await self.return_slots()
        await self.wait_for_move()
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        ipos, opos = tray.get_cell_position(idx)
        diff = opos - ipos
        slot_pos = diff - 5
        if tray.cell_width <= 50:
            await self.open_slots(1)
            await self._slot_1.move_to_position(slot_pos)
            await self._slot_2.move_to_position(slot_pos)
            await self._slot_3.move_to_position(slot_pos)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            await self.open_slots(2)
            await self._slot_1.move_to_position(slot_pos)
            await self._slot_2.move_to_position(slot_pos)
            await self._slot_3.move_to_position(slot_pos)
        else:
            await self.open_slots(3)
            await self._slot_1.move_to_position(slot_pos)
            await self._slot_2.move_to_position(slot_pos)
            await self._slot_3.move_to_position(slot_pos)
        ipos += self.offsets.funnel_cell_1_offset_x
        opos = self._configuration.opposing_funnel_max_travel - opos
        opos -= self.offsets.funnel_cell_1_offset_x
        opos += self.offsets.funnel_min_width
        opos = min([self._configuration.opposing_funnel_max_travel, opos])
        ipos = min([self._configuration.opposing_funnel_max_travel, ipos])
        opos = max([0, opos])
        curr_opos = await self._opposing_funnel.get_position()

        if tray.cell_width <= 50:
            await self.set_diverter_size(FunnelOpeningSize.OPEN_50MM)
        elif tray.cell_width > 50 and tray.cell_width <= 105:
            await self.set_diverter_size(FunnelOpeningSize.OPEN_100MM)
        else:
            await self.set_diverter_size(FunnelOpeningSize.OPEN_150MM)

        if opos >= curr_opos:
            await self._opposing_funnel.move_to_position(opos)
            await self._inlet_funnel.move_to_position(ipos)
            await self.wait_for_move()
        else:
            await self._inlet_funnel.move_to_position(ipos)
            await self._opposing_funnel.move_to_position(opos)
            await self.wait_for_move()

    async def return_tray_slots(self, tray: Tray, idx: int) -> None:
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        ipos, opos = tray.get_cell_position(idx)
        diff = opos - ipos
        slot_pos = diff - 5
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
        await self.inlet_funnel.move_to_position(0)
        await self.inlet_funnel.wait_for_move()
        await self.opposing_funnel.move_to_position(position)
        await self.return_slots()

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
        # self.return_slots()
        position = tray.cell_width - self.offsets.funnel_min_height
        if position >= 150:
            position = 148
        _, opos = tray.get_cell_position(idx)
        await self.open_tray_slots(tray)
        await self.open_tray_wedges(tray)
        opos = self._configuration.opposing_funnel_max_travel - opos
        opos -= self.offsets.funnel_cell_1_offset_x
        opos -= self.offsets.funnel_min_width
        opos = min([self._configuration.opposing_funnel_max_travel, opos])
        opos = max([0, opos])
        await self._opposing_funnel.move_to_position(opos)
        await self.wait_for_move()
