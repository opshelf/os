import axios from "axios";
import { HOST } from "./common";
import { RobotCommands, robotCommandsSchema } from "@/domain/commands/robot";
import { BaseMessageSchema } from "@/domain/commands/base";
import { RobotConfiguration, robotConfigurationSchema } from "@/domain/configuration";
import { StorageModuleData } from "@/domain";
import { v4 as uuidv4 } from "uuid";
import z from "zod";
// import { RobotConfiguration } from "@/domain/configuration";

export const createBaseMessage = (): BaseMessageSchema => {
  return { message_id: uuidv4() };
};

type GS1Payload = {
  raw_text: string;
  [key: string]: unknown;
};

export const storeCommand = async (gs1: GS1Payload): Promise<void> => {
  const response = await axios.post(`${HOST}/robot/events`, {
    kind: "storing_requested",
    payload: {
      gs1: gs1.raw_text,
    },
  });
  console.log(response.data);
};

export const robotCommand = async (command: RobotCommands): Promise<void> => {
  console.log(command);
  const response = await axios.post(`${HOST}/robot/commands`, command);
  console.log(response.data);
};

export const startWorker = async () => {
  await axios.post(`${HOST}/robot/start`);
};

export const clearCommands = async () => {
  await axios.post(`${HOST}/robot/clear_commands`);
};

export const getCommands = async (): Promise<RobotCommands[]> => {
  const response = await axios.get(`${HOST}/robot/commands`);
  return z.array(robotCommandsSchema).parse(response.data);
};

export const getConfiguration = async (): Promise<RobotConfiguration> => {
  const response = await axios.get(`${HOST}/robot/configuration`);
  return robotConfigurationSchema.parse(response.data);
};

export const setConfiguration = async (
  configuration: RobotConfiguration,
): Promise<RobotConfiguration> => {
  const response = await axios.post(
    `${HOST}/robot/configuration`,
    configuration,
  );
  return robotConfigurationSchema.parse(response.data);
};

export const setStorage = async (storage: StorageModuleData) => {
  const response = await axios.post(`${HOST}/robot/storage`, storage);
  return response.data;
};
