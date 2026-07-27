import queue
import threading


class AsyncFrameWriter:
    """Bounded producer/consumer wrapper around an OpenCV VideoWriter."""

    def __init__(self, writer, queue_size=4):
        self.writer = writer
        self.queue = queue.Queue(maxsize=queue_size)
        self.error = None
        self.closed = False
        self.thread = threading.Thread(
            target=self._write_loop,
            name="screen-recorder-writer",
        )
        self.thread.start()

    def _write_loop(self):
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    return
                frame, count = item
                for _ in range(count):
                    self.writer.write(frame)
        except Exception as exc:
            self.error = exc

    def submit(self, frame, count=1):
        if self.closed:
            raise RuntimeError("frame writer is closed")
        while True:
            if self.error is not None:
                raise self.error
            try:
                self.queue.put((frame, count), timeout=0.1)
                return
            except queue.Full:
                if not self.thread.is_alive():
                    raise RuntimeError("frame writer stopped unexpectedly")

    def close(self):
        if self.closed:
            return
        self.closed = True
        while self.thread.is_alive():
            try:
                self.queue.put(None, timeout=0.1)
                break
            except queue.Full:
                if self.error is not None:
                    break
        self.thread.join()
        self.writer.release()
        self.writer = None
        if self.error is not None:
            raise self.error
