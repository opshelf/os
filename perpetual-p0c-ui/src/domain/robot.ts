import z from 'zod';
import { gantryStatus } from './gantry';
import { v4 as uuidv4 } from 'uuid';
import { BaseMessageSchema } from './commands/base';
import { RobotCommands } from './commands/robot';
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

export const subsystemStatus = z.object({
  gantry: gantryStatus.nullable(),
  //tool:
  //io:
  //funnel:
});

export type SubsystemStatus = z.infer<typeof subsystemStatus>;

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
}
