import { v4 as uuidv4 } from "uuid";
import { BaseMessageSchema } from "./commands/base";
import { AxisCommandPayload, RobotCommands } from "./commands/robot";
import {
  CellItem,
  StorageModuleCellLocation,
  StorageModuleTrayLocationSchema,
} from "./storage";
// import { Tool, ToolCommands } from "./commands/tool";
import { Gantry, GantryCommands } from "./commands/gantry";

export const createBaseMessage = (): BaseMessageSchema => {
  return { message_id: uuidv4() };
};

export class RobotController {
  sendCommand: (command: RobotCommands) => Promise<void>;
  gantry: Gantry;
  // tool: Tool;
  constructor(sendCommand: (command: RobotCommands) => Promise<void>) {
    this.sendCommand = sendCommand;
    this.gantryCommand = this.gantryCommand.bind(this);
    this.gantry = new Gantry(this.gantryCommand);

    // this.toolCommand = this.toolCommand.bind(this);
    // this.tool = new Tool(this.toolCommand);
  }

  async storeItem(
    location: StorageModuleCellLocation,
    item: CellItem | null = null,
  ) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "store_item",
      payload: {
        location,
        item,
      },
    });
  }

  async retrieveItem(location: StorageModuleCellLocation) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "retrieve_item",
      payload: {
        location,
      },
    });
  }

  async home() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "home",
    });
  }

  async stop() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "stop",
    });
  }

  async homeYFirst() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "home_y_first",
    });
  }

  async initialize() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "initialize",
    });
  }

  async returnCurrentTray() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "return_current_tray",
    });
  }

  async pullTray(location: StorageModuleTrayLocationSchema) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "pull_tray",
      payload: {
        ...location,
      },
    });
  }

  async placeTray(location: StorageModuleTrayLocationSchema) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "place_tray",
      payload: {
        ...location,
      },
    });
  }

  // async toolCommand(command: ToolCommands) {
  //   await this.sendCommand({
  //     ...createBaseMessage(),
  //     command: "tool_command",
  //     payload: {
  //       ...command,
  //     },
  //   });
  // }

  async gantryCommand(command: GantryCommands) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "gantry_command",
      payload: {
        ...command,
      },
    });
  }

  async idlePositionCommand() {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "idle_position",
    });
  }

  async lowLevelAxisCommand(payload: AxisCommandPayload) {
    await this.sendCommand({
      ...createBaseMessage(),
      command: "axis_command",
      payload: payload,
    });
  }
}
