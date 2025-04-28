import { v4 as uuidv4 } from "uuid";
import { BaseMessageSchema } from "./commands/base";
import { RobotCommands } from "./commands/robot";
import { CellItem, StorageModuleCellLocation } from "./storage";
import { Tool, ToolCommands } from "./commands/tool";
import { Gantry, GantryCommands } from "./commands/gantry";
import { GantryCoordinateSchema } from "./movement";

export const createBaseMessage = (): BaseMessageSchema => {
  return { message_id: uuidv4() };
};

export class RobotController {
  sendCommand: (command: RobotCommands) => Promise<void>;
  gantry: Gantry;
  tool: Tool;

  constructor(sendCommand: (command: RobotCommands) => Promise<void>) {
    this.sendCommand = sendCommand;
    this.gantryCommand = this.gantryCommand.bind(this);
    this.gantry = new Gantry(this.gantryCommand);

    this.toolCommand = this.toolCommand.bind(this);
    this.tool = new Tool(this.toolCommand);
  }

  async storeItem(
    location: StorageModuleCellLocation,
    _item: CellItem | null = null,
  ) {
    await this.sendCommand({
      command: "store_item",
      location,
    });
  }

  async retrieveItem(location: StorageModuleCellLocation) {
    await this.sendCommand({
      command: "retrieve_item",
      location,
    });
  }

  async home() {
    await this.sendCommand({
      command: "home",
    });
  }

  async stop() {
    await this.sendCommand({
      command: "stop",
    });
  }

  async initialize() {
    await this.sendCommand({
      command: "initialize",
    });
  }

  async idlePositionCommand() {
    await this.sendCommand({
      command: "idle",
    });
  }

  async unsafeMove(coordinate: GantryCoordinateSchema) {
    await this.sendCommand({
      command: "unsafe_move",
      coordinate,
    });
  }

  async move(coordinate: GantryCoordinateSchema) {
    await this.sendCommand({
      command: "move",
      coordinate,
    });
  }

  async grabBox(coordinate: GantryCoordinateSchema, width: number) {
    await this.sendCommand({
      command: "grab_box",
      coordinate,
      width,
    });
  }

  async placeBox(coordinate: GantryCoordinateSchema, width: number) {
    await this.sendCommand({
      command: "place_box",
      coordinate,
      width,
    });
  }

  async moveToPickupLocation() {
    await this.sendCommand({
      command: "move_to_pickup_location",
    });
  }

  async dropOffItem() {
    await this.sendCommand({
      command: "drop_off_item",
    });
  }

  async pickupItem() {
    await this.sendCommand({
      command: "pickup_item",
    });
  }

  async returnCell() {
    await this.sendCommand({
      command: "return_cell",
    });
  }

  async grabCell(location: StorageModuleCellLocation) {
    await this.sendCommand({
      command: "grab_cell",
      location,
    });
  }

  async placeCell(location: StorageModuleCellLocation) {
    await this.sendCommand({
      command: "place_cell",
      location,
    });
  }

  async toolCommand(command: ToolCommands) {
    await this.sendCommand({
      command: "tool_command",
      payload: command,
    });
  }

  async gantryCommand(command: GantryCommands) {
    await this.sendCommand({
      command: "gantry_command",
      payload: command,
    });
  }
}
