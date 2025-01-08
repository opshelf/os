import axios from 'axios';
import { HOST } from './common';
import {
  RobotCommands,
  robotCommandsSchema,
} from '@/domain/commands/robot';
import { BaseMessage } from '@/domain/commands/funnel';
import { v4 as uuidv4 } from 'uuid';
import z from 'zod';

export const createBaseMessage = (): BaseMessage => {
  return { message_id: uuidv4() };
};

export const robotCommand = async (
  command: RobotCommands
): Promise<void> => {
  console.log(command);
  const response = await axios.post(
    `${HOST}/robot/commands`,
    command
  );
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
