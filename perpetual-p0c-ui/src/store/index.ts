import { create } from 'zustand';
import { GS1 } from '@/domain';
import { WorkerStatus } from '@/domain/status';

interface State {
  robotStatus: WorkerStatus | null;
  setStatus: (status: WorkerStatus) => void;
  gs1: GS1 | null;
  addGs1: (gs1: GS1 | null) => void;
}

export const useStateStore = create<State>((set) => ({
  robotStatus: null,
  gs1: null,
  setStatus: (status: WorkerStatus) =>
    set(() => ({ robotStatus: status })),
  addGs1: (gs1: GS1 | null) => set(() => ({ gs1 })),
}));
