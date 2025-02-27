import cv2
import threading
import time
import queue
from typing import Callable, List, Optional, Any, Self
import logging


class AsyncCameraReader:
    """
    A thread-safe class to read camera frames asynchronously using OpenCV.
    Uses a queue to safely transfer frames between threads.
    """

    def __init__(
        self, camera_id: int = 0, fps_limit: Optional[int] = None, queue_size: int = 10
    ) -> None:
        """
        Initialize the camera reader.

        Args:
            camera_id: The ID of the camera to use (default: 0)
            fps_limit: Optional limit on frames per second (default: None, no limit)
            queue_size: Maximum number of frames to keep in the queue (default: 10)
        """
        self.camera_id = camera_id
        self.fps_limit = fps_limit
        self.min_frame_time = 1.0 / fps_limit if fps_limit else 0
        self.queue_size = queue_size

        self.callbacks: List[Callable[[cv2.typing.MatLike], None]] = []
        self.capture: Optional[cv2.VideoCapture] = None
        self._running = False
        self._capture_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self.last_frame_time = 0.0

        # Queue for safely passing frames between threads
        # self.frame_queue = queue.Queue(maxsize=queue_size)

        self.frame_queue: queue.Queue[cv2.typing.MatLike] = queue.Queue(maxsize=queue_size)

    def start(self) -> bool:
        """
        Start capturing frames from the camera in a background thread.

        Returns:
            bool: True if started successfully, False otherwise.
        """
        if self._running:
            return True
        self.capture = cv2.VideoCapture(self.camera_id)
        if not self.capture.isOpened():
            self.capture = None
            return False

        self._running = True

        # Start the capture thread
        self._capture_thread = threading.Thread(target=self._capture_loop)
        self._capture_thread.daemon = True
        self._capture_thread.start()

        # Start the processing thread
        self._process_thread = threading.Thread(target=self._process_loop)
        self._process_thread.daemon = True
        self._process_thread.start()

        return True

    def stop(self) -> None:
        """Stop the background threads and release the camera."""
        self._running = False

        # Join threads
        if self._capture_thread:
            self._capture_thread.join(timeout=1.0)
            self._capture_thread = None

        if self._process_thread:
            self._process_thread.join(timeout=1.0)
            self._process_thread = None

        # Release camera
        if self.capture:
            self.capture.release()
            self.capture = None

    def add_callback(self, callback: Callable[[cv2.typing.MatLike], None]) -> None:
        """
        Add a callback function to be called for each frame.

        Args:
            callback: A function that takes a frame as its argument.
        """
        if callback not in self.callbacks:
            self.callbacks.append(callback)

    def remove_callback(self, callback: Callable[[cv2.typing.MatLike], None]) -> None:
        """
        Remove a previously registered callback function.

        Args:
            callback: The callback function to remove.
        """
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def _capture_loop(self) -> None:
        """
        The capture loop that runs in a background thread.
        Captures frames and puts them in the queue.
        """
        while self._running and self.capture and self.capture.isOpened():
            current_time = time.time()
            elapsed = current_time - self.last_frame_time

            # Apply FPS limiting if needed
            if self.fps_limit and elapsed < self.min_frame_time:
                time.sleep(self.min_frame_time - elapsed)
                continue

            ret, frame = self.capture.read()
            if not ret:
                break

            self.last_frame_time = time.time()

            # Put the frame in the queue, but don't block if queue is full
            try:
                self.frame_queue.put(frame.copy(), block=False)
            except queue.Full:
                # If the queue is full, skip this frame
                pass

    def _process_loop(self) -> None:
        """
        The processing loop that runs in a background thread.
        Takes frames from the queue and calls the callbacks.
        """
        while self._running:
            try:
                # Get a frame from the queue with a timeout
                frame = self.frame_queue.get(timeout=0.1)

                # Call all registered callbacks with the frame
                for callback in self.callbacks:
                    try:
                        callback(frame)
                    except Exception as e:
                        print(f"Error in callback: {e}")

                # Mark the task as done
                self.frame_queue.task_done()

            except queue.Empty:
                # No frames in the queue, just continue
                continue

    def __enter__(self) -> Self:
        """Context manager support."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:  # type: ignore
        """Ensure the camera is released when the context manager exits."""
        logging.error(f"{exc_type}, {exc_val}, {exc_tb}")
        self.stop()
