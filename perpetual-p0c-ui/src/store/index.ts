import { create } from 'zustand';
import { SubsystemStatus } from '@/domain/robot';
import { GS1 } from '@/domain';

//import { Status } from '@/domain';
//import { StatusUpdate } from "@/domain";

//interface SubsystemStatus {
//  status: SubsystemStatus;
//  setSubsystemStatus: (status: SubsystemStatus) => void;
//}

interface State {
  subsystemStatus: SubsystemStatus;
  setStatus: (status: SubsystemStatus) => void;
  gs1: GS1 | null;
  addGs1: (gs1: GS1 | null) => void;
}

export const useStateStore = create<State>((set) => ({
  subsystemStatus: {
    gantry: null,
  },
  gs1: null,
  setStatus: (status: SubsystemStatus) =>
    set(() => ({ subsystemStatus: status })),
  addGs1: (gs1: GS1 | null) => set(() => ({ gs1 })),
}));
