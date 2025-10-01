import { useEffect, useRef } from "react";
import { useStateStore } from "../../store";
import { workerStatusSchema } from "@/domain/status";
import { HOST } from "@/api/common";

const RobotStatusEventSSE = () => {
  const maxReconnectAttempts = 1000;
  const reconnectInterval = 3000;
  const reconnectAttemptsRef = useRef(0);

  useEffect(() => {
    let eventSource: EventSource | null = null;
    let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;

    const connect = () => {
      const store = useStateStore.getState();
      store.setConnectionStatus('reconnecting');

      eventSource = new EventSource(`${HOST}/robot/status`);

      eventSource.onopen = () => {
        store.setConnectionStatus('connected');
        store.resetReconnectAttempts();
        reconnectAttemptsRef.current = 0;
      };

      eventSource.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          const status = workerStatusSchema.parse(data);
          store.setStatus(status);

          if (status.error_queue && status.error_queue.length > 0) {
            store.setErrors(status.error_queue);
          }
        } catch (err) {
          console.error("Failed to parse robot status:", err);
          store.addError({
            timestamp: new Date().toISOString(),
            error_type: "StatusParseError",
            message: "Failed to parse robot status update",
            context: { error: err instanceof Error ? err.message : String(err) }
          });
        }
      };

      eventSource.onerror = () => {
        if (eventSource) {
          eventSource.close();
          eventSource = null;
          store.setConnectionStatus('disconnected');

          if (reconnectAttemptsRef.current < maxReconnectAttempts) {
            reconnectAttemptsRef.current += 1;
            store.incrementReconnectAttempts();
            reconnectTimeout = setTimeout(() => {
              connect();
            }, reconnectInterval);
          } else {
            console.error("Maximum reconnect attempts reached. Giving up.");
            store.addError({
              timestamp: new Date().toISOString(),
              error_type: "ConnectionError",
              message: "Maximum reconnection attempts reached. Connection to robot status failed.",
              context: { attempts: reconnectAttemptsRef.current }
            });
          }
        }
      };
    };

    connect();

    return () => {
      if (eventSource) {
        eventSource.close();
      }
      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
      }
      useStateStore.getState().setConnectionStatus('disconnected');
    };
  }, []);

  return null;
};

export default RobotStatusEventSSE;
