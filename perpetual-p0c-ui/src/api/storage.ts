import axios from 'axios';
import { HOST } from './common';
import { storageModuleSchema, StorageModuleData } from '@/domain';

export const updateStorage = async (
  data: StorageModuleData
): Promise<StorageModuleData> => {
  const response = await axios.put(`${HOST}/storage`, data);
  return storageModuleSchema.parse(response.data);
};

export const getStorage = async (): Promise<StorageModuleData> => {
  const response = await axios.get(`${HOST}/storage`);
  return storageModuleSchema.parse(response.data);
};
