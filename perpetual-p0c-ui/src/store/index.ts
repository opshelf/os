import { create } from 'zustand';
import { SubsystemStatus } from '@/domain/robot';

//import { Status } from '@/domain';
//import { StatusUpdate } from "@/domain";

//interface SubsystemStatus {
//  status: SubsystemStatus;
//  setSubsystemStatus: (status: SubsystemStatus) => void;
//}

interface State {
  subsystemStatus: SubsystemStatus;
  setStatus: (status: SubsystemStatus) => void;
}

export const useStateStore = create<State>((set) => ({
  subsystemStatus: {
    gantry: null,
  },
  setStatus: (status: SubsystemStatus) =>
    set(() => ({ subsystemStatus: status })),
}));
