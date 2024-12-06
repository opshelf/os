import axios from 'axios';
import { HOST } from './common';
import { ToolCommands } from '@/domain/tool';

export const toolCommand = async (
  data: ToolCommands
): Promise<void> => {
  await axios.post(`${HOST}/tool`, data);
};
