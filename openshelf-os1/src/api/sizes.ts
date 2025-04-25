import axios from "axios";
import { HOST } from "./common";
import { SizeDatabase, sizeDatabaseSchema, ItemInformation } from "@/domain";

export const getSizes = async (): Promise<SizeDatabase> => {
  const result = await axios.get(`${HOST}/sizes`);
  return sizeDatabaseSchema.parse(result.data);
};

export const updateSizes = async (data: {
  upca: string;
  item: ItemInformation;
}): Promise<SizeDatabase> => {
  const result = await axios.put(`${HOST}/sizes`, data);
  return sizeDatabaseSchema.parse(result.data);
};

export const deleteSizes = async (upca: string): Promise<SizeDatabase> => {
  const result = await axios.delete(`${HOST}/sizes/${upca}`);
  return sizeDatabaseSchema.parse(result.data);
};

export const addItem = async (item: ItemInformation): Promise<SizeDatabase> => {
  const result = await axios.post(`${HOST}/sizes`, item);
  return sizeDatabaseSchema.parse(result.data);
};
