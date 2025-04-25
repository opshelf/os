import axios from "axios";
import { HOST } from "./common";
import { Dimension } from "@/domain";

export const print = async (
  dimensions: Dimension,
  data: any,
): Promise<void> => {
  await axios.post(`${HOST}/print`, {
    dimensions,
    item: data,
  });
};

export const printUpca = async (upca: string): Promise<void> => {
  await axios.post(`${HOST}/print/${upca}`);
};
