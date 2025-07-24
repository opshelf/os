import { create } from "zustand";
import { WorkerStatus } from "@/domain/status";

type ScaleReading = {
  weight: number;
  timestamp: string;
};

export interface WorkerError {
  timestamp: string;
  error_type: string;
  message: string;
  context?: Record<string, unknown>;
  stack_trace?: string;
}

export type ConnectionStatus = 'connected' | 'disconnected' | 'reconnecting';

interface State {
  robotStatus: WorkerStatus | null;
  setStatus: (status: WorkerStatus) => void;
  reading: ScaleReading | null;
  addReading: (reading: ScaleReading) => void;
  scan: string | null;
  addScan: (scan: string) => void;
  // Error management
  errors: WorkerError[];
  connectionStatus: ConnectionStatus;
  reconnectAttempts: number;
  lastErrorTimestamp?: number;
  addError: (error: WorkerError) => void;
  setErrors: (errors: WorkerError[]) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  incrementReconnectAttempts: () => void;
  resetReconnectAttempts: () => void;
}

export const useStateStore = create<State>((set) => ({
  robotStatus: null,
  setStatus: (status: WorkerStatus) => set(() => ({ robotStatus: status })),
  reading: null,
  addReading: (reading: ScaleReading) => set(() => ({ reading })),
  scan: null,
  addScan: (scan: string) => set(() => ({ scan })),
  // Error management
  errors: [],
  connectionStatus: 'disconnected',
  reconnectAttempts: 0,
  lastErrorTimestamp: undefined,
  addError: (error: WorkerError) => set((state) => ({
    errors: [...state.errors.slice(-9), error], // Keep last 10 errors
    lastErrorTimestamp: Date.now(),
  })),
  setErrors: (errors: WorkerError[]) => set(() => ({ 
    errors: errors.slice(-10), // Keep last 10 errors
    lastErrorTimestamp: errors.length > 0 ? Date.now() : undefined,
  })),
  setConnectionStatus: (status: ConnectionStatus) => set(() => ({ connectionStatus: status })),
  incrementReconnectAttempts: () => set((state) => ({ 
    reconnectAttempts: state.reconnectAttempts + 1 
  })),
  resetReconnectAttempts: () => set(() => ({ reconnectAttempts: 0 })),
}));
