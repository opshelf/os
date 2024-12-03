import axios from 'axios';
import { HOST } from './common';
//import {
//  GantryCoordinateSchema,
//  ToolCoordinateSchema,
//} from '@/domain';
import {
  GantrySubsystemCommands,
  GantryMoveCommands,
} from '@/domain/gantry';

export const moveGantry = async (
  data: GantryMoveCommands
): Promise<void> => {
  await axios.post(`${HOST}/gantry`, data);
};

export const gantryCommand = async (
  data: GantrySubsystemCommands
): Promise<void> => {
  await axios.post(`${HOST}/gantry`, data);
};

//export const moveTool = async (
//  data: ToolCoordinateSchema
//): Promise<void> => {
//  await axios.post(`${HOST}/move`, data);
//};
