import { useEffect, useState } from "react";
import { useStateStore } from "../../store";
import { scaleEventSchema } from "@/domain";
import { HOST } from "@/api/common";

const ScaleEventSSE = () => {
  const [reconnectAttempts, setReconnectAttempts] = useState(0);
  const maxReconnectAttempts = 1000;
  const reconnectInterval = 3000; // 3 seconds

  useEffect(() => {
    let eventSource: EventSource | null = null;
    let reconnectTimeout: any = null;

    const connect = () => {
      eventSource = new EventSource(`${HOST}/scale/readings`);

      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          const scaleData = scaleEventSchema.parse(data);
          useStateStore.getState().addReading(scaleData.payload);
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

export default ScaleEventSSE;
