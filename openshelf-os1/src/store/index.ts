import { create } from "zustand";
import { WorkerStatus } from "@/domain/status";

interface State {
  robotStatus: WorkerStatus | null;
  setStatus: (status: WorkerStatus) => void;
}

export const useStateStore = create<State>((set) => ({
  robotStatus: null,
  setStatus: (status: WorkerStatus) => set(() => ({ robotStatus: status })),
}));
