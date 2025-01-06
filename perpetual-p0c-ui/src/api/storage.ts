import axios from 'axios';
import { HOST } from './common';
import z from 'zod';
import {
  storageModuleSchema,
  StorageModuleData,
  StorageModuleCellLocation,
  storageModuleCellLocationSchema,
  CellLocationData,
  cellLocationDataSchema,
} from '@/domain';

export const updateStorage = async (
  data: StorageModuleData
): Promise<StorageModuleData> => {
  const response = await axios.put(`${HOST}/storage/storage`, data);
  return storageModuleSchema.parse(response.data);
};

export const getStorage = async (): Promise<StorageModuleData> => {
  const response = await axios.get(`${HOST}/storage/storage`);
  return storageModuleSchema.parse(response.data);
};

export const getFreeCells = async (): Promise<
  StorageModuleCellLocation[]
> => {
  const response = await axios.get(`${HOST}/storage/locations`);
  return z
    .array(storageModuleCellLocationSchema)
    .parse(response.data);
};

export const getItems = async (): Promise<CellLocationData[]> => {
  const response = await axios.get(`${HOST}/storage/items`);
  return z.array(cellLocationDataSchema).parse(response.data);
};
