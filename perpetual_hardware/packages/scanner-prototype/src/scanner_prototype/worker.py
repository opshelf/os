import threading
import queue
from perpetual_p0c.robot import P0C

# from .mock import MockP0C
from perpetual_p0c.configuration import p0_robot_config
from perpetual_p0c.storage import CellItem, Dimensions, GS1Data, StorageModuleCellLocation
from .commands import BaseCommand, Status


class RobotWorker(threading.Thread):
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs) -> "RobotWorker":  # type: ignore
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self, incoming_queue: "queue.Queue[BaseCommand]", processed_queue: "queue.Queue[Status]"
    ) -> None:
        if not hasattr(self, "initialized"):
            threading.Thread.__init__(self)
            self._incoming_queue = incoming_queue
            self._processed_queue = processed_queue
            # self._robot = P0C(p0_robot_config)
            self._robot = P0C(p0_robot_config)
            self._robot.home()
            self.daemon = True
            self.initialized = True
            self._stop_event = threading.Event()

    @property
    def robot(self) -> P0C:  # type: ignore
        return self._robot

    def shutdown(self) -> None:
        # if self._robot:
        self._robot.shutdown()
        # if self._bus:
        #    self._bus.shutdown()

    """
    def connect(self) -> None:
        try:
            if not self._connected:
                self._bus = get_bus()
                self._robot = P0Robot(self._bus, p0_robot_config)
                self._robot.init()
                self._connected = True
        except Exception as e:
            print(e)
            self._connected = False

    def disconnect(self) -> None:
        try:
            if self._robot:
                self._robot.stop()
                self._robot = None
            if self._bus:
                self._bus.shutdown()
                self._bus = None
            self._connected = False
        except Exception as e:
            print(e)
    """

    # def send_status_update(self) -> None:
    #    #status = self.status()
    #    #if status:
    #    #    self._processed_queue.put(status)
    #    pass

    # def status(self) -> Optional[Status]:
    #    try:
    #        return Status(position=self._robot.positions, magnet=self._robot.magnet_state)
    #    except Exception as e:
    #        print(e
    def run(self) -> None:
        while not self._stop_event.is_set():
            if self._robot:
                try:
                    message: BaseCommand = self._incoming_queue.get(
                        timeout=1
                    )  # Timeout to check stop event periodically
                    print(f"Thread {message}")
                    if message is None:
                        break
                    try:
                        print(f"Task Ended: {message}")
                        if message.command == "STORE_ITEM":
                            if message.payload:
                                cmd = GS1Data(**message.payload)
                                dimensions = self._robot.size_db.get_item_size(cmd)
                                if dimensions:
                                    self._robot.store_item(
                                        CellItem(dimensions=dimensions, gs1_data=cmd)
                                    )
                                    if self._incoming_queue.qsize():
                                        pass
                                    else:
                                        self._robot.return_current_tray()
                                else:
                                    raise Exception("No Known Size")
                            else:
                                raise Exception("Invalid Command")
                        elif message.command == "RETRIEVE_ITEM_GS1":
                            if message.payload:
                                gs1 = GS1Data(**message.payload)
                                self._robot.retrieve_item_by_gtin(gs1)
                                if self._incoming_queue.qsize():
                                    pass
                                else:
                                    self._robot.return_current_tray()
                            else:
                                raise Exception("Invalid Command")
                        elif message.command == "RETRIEVE_ITEM_LOCATION":
                            if message.payload:
                                smcl = StorageModuleCellLocation(**message.payload)
                                self._robot.retrieve_item_by_location(smcl)
                                if self._incoming_queue.qsize():
                                    pass
                                else:
                                    self._robot.return_current_tray()
                            else:
                                raise Exception("Invalid Command")
                        else:
                            raise Exception("Invalid Command")
                    except Exception as ee:
                        print("Worker Error: ", ee)
                except Exception as e:
                    if isinstance(e, queue.Empty):
                        # self.send_status_update()
                        pass
                    else:
                        print("Unexpected Error: ", e)
