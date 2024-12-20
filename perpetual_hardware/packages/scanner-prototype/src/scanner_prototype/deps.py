from queue import Queue
from .commands import BaseCommand, Status
from .worker import RobotWorker


incoming_message_queue: "Queue[BaseCommand]" = Queue()
processed_message_queue: "Queue[Status]" = Queue()

type CommandQueue = "Queue[BaseCommand]"
type StatusQueue = "Queue[Status]"


def get_incoming_message_queue() -> CommandQueue:
    return incoming_message_queue


def get_processed_message_queue() -> StatusQueue:
    return processed_message_queue


def get_worker() -> RobotWorker:
    worker = RobotWorker(incoming_message_queue, processed_message_queue)
    if not worker.is_alive():
        worker.start()
    return worker
