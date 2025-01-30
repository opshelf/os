import { v4 as uuidv4 } from 'uuid';
import { BaseMessageSchema } from './commands/base';
import { AxisCommandPayload, RobotCommands } from './commands/robot';
import {
  CellItem,
  StorageModuleCellLocation,
  StorageModuleTrayLocationSchema,
} from './storage';
import { Tool, ToolCommands } from './commands/tool';
import { Funnel, FunnelCommands } from './commands/funnel';
import {
  Workstation,
  WorkstationCommands,
} from './commands/workstation';
import { Gantry, GantryCommands } from './commands/gantry';

export const createBaseMessage = (): BaseMessageSchema => {
  return { message_id: uuidv4() };
};

export class RobotController {
  sendCommand: (command: RobotCommands) => Promise<void>;
  gantry: Gantry;
  funnel: Funnel;
  tool: Tool;
  workstation: Workstation;
  constructor(
    sendCommand: (command: RobotCommands) => Promise<void>
  ) {
    this.sendCommand = sendCommand;
    this.gantryCommand = this.gantryCommand.bind(this);
    this.gantry = new Gantry(this.gantryCommand);
    this.funnelCommand = this.funnelCommand.bind(this);
    this.funnel = new Funnel(this.funnelCommand);

    this.toolCommand = this.toolCommand.bind(this);
    this.tool = new Tool(this.toolCommand);
    this.workstationCommand = this.workstationCommand.bind(this);
    this.workstation = new Workstation(this.workstationCommand);
  }

  async storeItem(
    location: StorageModuleCellLocation,
    item: CellItem | null = null
  ) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'store_item',
      payload: {
        location,
        item,
      },
    });
  }

  async retrieveItem(location: StorageModuleCellLocation) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'retrieve_item',
      payload: {
        location,
      },
    });
  }

  async home() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'home',
    });
  }

  async stop() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'stop',
    });
  }

  async homeYFirst() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'home_y_first',
    });
  }

  async initialize() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'initialize',
    });
  }

  async returnCurrentTray() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'return_current_tray',
    });
  }

  async pullTray(location: StorageModuleTrayLocationSchema) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'pull_tray',
      payload: {
        ...location,
      },
    });
  }

  async placeTray(location: StorageModuleTrayLocationSchema) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'place_tray',
      payload: {
        ...location,
      },
    });
  }

  async toolCommand(command: ToolCommands) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'tool_command',
      payload: {
        ...command,
      },
    });
  }

  async funnelCommand(command: FunnelCommands) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'funnel_command',
      payload: {
        ...command,
      },
    });
  }

  async workstationCommand(command: WorkstationCommands) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'workstation_command',
      payload: {
        ...command,
      },
    });
  }

  async gantryCommand(command: GantryCommands) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'gantry_command',
      payload: {
        ...command,
      },
    });
  }

  async idlePositionCommand() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'idle_position',
    });
  }

  async serviceHighPosition() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'service_high_position',
    });
  }

  async lowLevelAxisCommand(payload: AxisCommandPayload) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: 'axis_command',
      payload: payload,
    });
  }
}

/*
class StatusFlags(BaseModel):
  value: int
  stall_guard2_status: bool = Field(default=False)
  over_temperature: bool = Field(default=False)
  over_temperature_pre_warning: bool = Field(default=False)
  short_to_ground_a: bool = Field(default=False)
  short_to_ground_b: bool = Field(default=False)
  open_load_a: bool = Field(default=False)
  open_load_b: bool = Field(default=False)
  stand_still: bool = Field(default=False)

class TrinamicErrorFlags(BaseModel):
  value: int
  stall_guard_error: bool = Field(default=False)
  deviation_error: bool = Field(default=False)

class AxisStatus(BaseModel):
  status_flags: StatusFlags
  error_flags: TrinamicErrorFlags
  position: float
  homed: HomedStatus
  hard_stopped: HardStopStatus

class StepperStatus(BaseModel):
  status_flags: StatusFlags
  error_flags: TrinamicErrorFlags
  position: float
  homed: HomedStatus
  hard_stopped: HardStopStatus

class RobotStatus(BaseModel):
  gantry_status: GantryStatus
  workstation_status: WorkstationStatus
  funnel_status: FunnelStatus
  tool_status: ToolStatus

class ToolStatus(BaseModel):
  puller_solenoids: int
  carriage_solenoids: int
  actuators: bool
  carriage_status: AxisStatus

class OutputBoxStatus(BaseModel):
  door_status: AxisStatus

class GantryStatus(BaseModel):
  x_axis: AxisStatus
  y_axis: AxisStatus

class FunnelStatus(BaseModel):
  inlet_funnel: AxisStatus
  opposing_funnel: AxisStatus
  slot_1: AxisStatus
  slot_2: AxisStatus
  slot_3: AxisStatus
  belt: StepperStatus
  wall: StepperStatus
  diverter: StepperStatus
  output_transfer_conveyor: StepperStatus
  output_conveyor: StepperStatus

class WorkstationStatus(BaseModel):
  output_box_1: OutputBoxStatus
  output_box_2: OutputBoxStatus
  output_box_3: OutputBoxStatus
  input_belt: AxisStatus
  output_belt: AxisStatus
*/
