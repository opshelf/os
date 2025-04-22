import { create } from "zustand";
import { WorkerStatus } from "@/domain/status";

type ScaleReading = {
  weight: number;
  timestamp: string;
};
interface State {
  robotStatus: WorkerStatus | null;
  setStatus: (status: WorkerStatus) => void;
  reading: ScaleReading | null;
  addReading: (reading: ScaleReading) => void;
  scan: string | null;
  addScan: (scan: string) => void;
}

export const useStateStore = create<State>((set) => ({
  robotStatus: null,
  setStatus: (status: WorkerStatus) => set(() => ({ robotStatus: status })),
  reading: null,
  addReading: (reading: ScaleReading) => set(() => ({ reading })),
  scan: null,
  addScan: (scan: string) => set(() => ({ scan })),
}));
