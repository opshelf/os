import { useEffect, useState } from 'react';
import { useBarCodeStore } from '../../store';

const AutoReconnectSSE = () => {
  const [reconnectAttempts, setReconnectAttempts] = useState(0);
  const maxReconnectAttempts = 10;
  const reconnectInterval = 3000; // 3 seconds

  useEffect(() => {
    let eventSource: EventSource | null = null;
    let reconnectTimeout: any = null;

    const connect = () => {
      eventSource = new EventSource(`http://localhost:8000/barcodes`);
      //src="http://localhost:8000/stream"

      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          console.log(data);
          useBarCodeStore.getState().addBarcode(data);
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
            console.error(
              'Maximum reconnect attempts reached. Giving up.'
            );
          }
        }
      };
    };

    connect();

    return () => {
      if (eventSource) {
        eventSource.close();
      }
      clearTimeout(reconnectTimeout);
    };
  }, [reconnectAttempts]);

  return <></>;
};

export default AutoReconnectSSE;
