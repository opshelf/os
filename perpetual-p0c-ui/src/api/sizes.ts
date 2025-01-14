import axios from 'axios';
import { HOST } from './common';
import z from 'zod';
import { Dimension, dimensionSchema, GS1 } from '@/domain';

export const updateSizes = async (
  data: GS1,
  dimensions: Dimension
): Promise<void> => {
  await axios.put(`${HOST}/sizes/sizes`, {
    gs1_data: data,
    dimensions,
  });
};

export const deleteSize = async (data: GS1): Promise<void> => {
  await axios.delete(`${HOST}/sizes/sizes`, {
    data: {
      gs1_data: data,
    },
  });
};

export const getSize = async (data: GS1): Promise<Dimension> => {
  const response = await axios.get(
    `${HOST}/sizes/sizes/${btoa(JSON.stringify(data))}`
  );
  console.log(response);
  return dimensionSchema.parse(response.data);
};

export const getSizes = async (): Promise<{
  items: { [key: string]: Dimension };
}> => {
  const response = await axios.get(`${HOST}/sizes/sizes`);
  const sizeDb = z.object({
    items: z.record(z.string(), dimensionSchema),
  });
  return sizeDb.parse(response.data);
};
