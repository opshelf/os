import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Button } from './components/ui/button';
import VideoOutput from './components/VideoOutput';
import AutoReconnectSSE from './components/AutoReconnectSSE';
import { BarCode, useBarCodeStore, GS1Data, UPCData } from './store';
import DimensionForm from '@/components/DimensionForm';

const GS1Card = ({ gs1 }: { gs1: GS1Data }) => {
  return (
    <Card className="w-full">
      <CardHeader>
        <CardTitle>GS1</CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          <li>{`GTIN: ${gs1.gtin}`}</li>
          <li>{`EXPIRY: ${gs1.expiry}`}</li>
          <li>{`LOT: ${gs1.lot}`}</li>
          <li>{`SN: ${gs1.sn}`}</li>
        </ul>
      </CardContent>
    </Card>
  );
};

const UPCACard = ({ upca }: { upca: UPCData }) => {
  return (
    <Card className="w-full max-w-md">
      <CardHeader>
        <CardTitle>UPCA</CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2">
          <li>{`TEXT: ${upca.text}`}</li>
        </ul>
      </CardContent>
    </Card>
  );
};

export default function App() {
  const { barcodes, clearItems } = useBarCodeStore();

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
        <div className="w-1/2 p-4 flex flex-col items-center space-y-2">
          {barcodes.length > 0 ? (
            <>
              {barcodes.map((barcode: BarCode) => {
                if ('gtin' in barcode) {
                  return <GS1Card gs1={barcode} />;
                } else {
                  return <UPCACard upca={barcode} />;
                }
              })}
              <DimensionForm data={barcodes[0] as GS1Data} />
            </>
          ) : (
            <p>No barcodes found.</p>
          )}
          <div className="flex grow" />
          <Button className="w-full self-end" onClick={clearItems}>
            RESET
          </Button>
        </div>
      </div>
    </div>
  );
}
