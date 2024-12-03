import axios from 'axios';
import { HOST } from './common';
import { FunnelCommands } from '@/domain/funnel';

export const funnelCommand = async (
  data: FunnelCommands
): Promise<void> => {
  await axios.post(`${HOST}/funnel`, data);
};
