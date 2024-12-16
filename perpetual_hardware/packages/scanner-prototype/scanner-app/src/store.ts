import { create } from 'zustand';

export type GS1Data = {
  gtin: string;
  expiry: string;
  lot: string;
  sn: string;
};

export type UPCData = {
  gtin: string;
};

export type BarCode = GS1Data | UPCData;

// Define the Zustand store interface
export interface BarCodeStore {
  barcodes: BarCode[]; // List of unique items
  addBarcode: (barcodes: BarCode) => void; // Add a unique item
  //removeItem: (gtin: string) => void; // Remove an item by gtin
  clearItems: () => void; // Clear all items
}

// Create the Zustand store
export const useBarCodeStore = create<BarCodeStore>((set, get) => ({
  barcodes: [],

  // Add an item ensuring uniqueness by `gtin`
  addBarcode: (item: BarCode) => {
    set((state: BarCodeStore) => {
      const isDuplicate = state.barcodes.some(
        (existingItem: BarCode) => existingItem.gtin === item.gtin
      );
      if (!isDuplicate) {
        return { barcodes: [...state.barcodes, item] };
      }
      return state;
    });
  },

  //// Remove an item by `gtin`
  //removeBarcode: (gtin: string) => {
  //  set((state: BarCodeStore) => ({
  //    barcodes: state.barcodes.filter((item) => item.gtin !== gtin),
  //  }));
  //},

  // Clear all items
  clearItems: () => {
    set({ barcodes: [] });
  },
}));
