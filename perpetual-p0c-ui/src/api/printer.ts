import axios from 'axios';
import { HOST } from './common';
import { GS1 } from '@/domain';

export const print = async (data: GS1): Promise<void> => {
  await axios.post(`${HOST}/printer/print`, {
    kind: 'label_printing_requested',
    payload: {
      gs1: data.raw_text,
      order_id: 'herererer',
      quantity: 30,
    },
  });
};
