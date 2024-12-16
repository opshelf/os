import { useEffect, useState } from 'react';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from './components/ui/button';
import VideoOutput from './components/VideoOutput';
import AutoReconnectSSE from './components/AutoReconnectSSE';
import { BarCode, useBarCodeStore } from './store';

export default function App() {
  const { barcodes, addItem, removeItem, clearItems } =
    useBarCodeStore();

  return (
    <div className="flex flex-col min-h-screen p-4">
      <AutoReconnectSSE />
      <span className="w-full text-center text-xl">
        Barcode Scanner Test App
      </span>
      <div className="flex grow p-4">
        {/* Left side: Display the image */}
        <div className="w-1/2 flex items-center justify-center border-r border-gray-200 p-4">
          <VideoOutput />
        </div>

        {/* Right side: Display the list of barcodes */}
        <div className="w-1/2 p-4 flex flex-col items-center">
          <Card className="w-full max-w-md">
            <CardHeader>
              <CardTitle>Barcodes</CardTitle>
            </CardHeader>
            <CardContent>
              {barcodes.length > 0 ? (
                <ul className="space-y-2">
                  {barcodes.map((barcode: BarCode, index: number) => (
                    <li key={index} className="border-b pb-2">
                      <p className="font-semibold">
                        Text: {barcode.gtin}
                      </p>
                      {/*
                    <p className="text-sm text-gray-500">
                      Format: {barcode.}
                    </p>
                    */}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>No barcodes found.</p>
              )}
            </CardContent>
          </Card>
          <div className="flex grow" />
          <Button className="w-full self-end" onClick={clearItems}>
            RESET
          </Button>
        </div>
      </div>
    </div>
  );
}
