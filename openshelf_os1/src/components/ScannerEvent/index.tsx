import { useEffect, useState } from "react";
import { useStateStore } from "../../store";
import { barcodeEventSchema } from "@/domain";
import { HOST } from "@/api/common";

const ScannerEventSSE = () => {
  const [reconnectAttempts, setReconnectAttempts] = useState(0);
  const maxReconnectAttempts = 1000;
  const reconnectInterval = 3000; // 3 seconds

  useEffect(() => {
    let eventSource: EventSource | null = null;
    let reconnectTimeout: any = null;

    const connect = () => {
      eventSource = new EventSource(`${HOST}/scanner/scans`);

      eventSource.onmessage = (event) => {
        try {
          console.log(event);
          const data = JSON.parse(event.data);
          const barcode = barcodeEventSchema.parse(data);
          useStateStore.getState().addScan(barcode.payload);
        } catch (err) {
          console.error(err);
        }
        setReconnectAttempts(0); // reset reconnect attempts on successful connection
      };

      eventSource.onerror = () => {
        if (eventSource) {
          eventSource.close();
          if (reconnectAttempts < maxReconnectAttempts) {
            setReconnectAttempts((prev) => prev + 1);
            reconnectTimeout = setTimeout(connect, reconnectInterval);
          } else {
            console.error("Maximum reconnect attempts reached. Giving up.");
          }
        }
      };
    };

    // connect();

    return () => {
      if (eventSource) {
        eventSource.close();
      }
      clearTimeout(reconnectTimeout);
    };
  }, [reconnectAttempts]);

  return <></>;
};

export default ScannerEventSSE;
