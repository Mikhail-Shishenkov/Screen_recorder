from __future__ import annotations

import collections
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from media_mux import MediaMuxError, find_ffmpeg


class FFmpegVideoError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoProfile:
    key: str
    label: str
    fps: int
    crf: int
    preset: str = "veryfast"
    max_width: int | None = None
    max_height: int | None = None


VIDEO_PROFILES = (
    VideoProfile(
        key="tracker",
        label="Для отправки — рекомендуется",
        fps=15,
        crf=28,
    ),
    VideoProfile(
        key="maximum",
        label="Максимальное качество",
        fps=24,
        crf=21,
    ),
    VideoProfile(
        key="compact",
        label="Компактный размер",
        fps=15,
        crf=28,
        max_width=1920,
        max_height=1080,
    ),
)
VIDEO_PROFILES_BY_KEY = {profile.key: profile for profile in VIDEO_PROFILES}


def get_video_profile(key):
    try:
        return VIDEO_PROFILES_BY_KEY[key]
    except KeyError as exc:
        raise ValueError(f"Unknown video profile: {key}") from exc


def calculate_output_size(width, height, profile):
    width = int(width)
    height = int(height)
    if width <= 0 or height <= 0:
        raise ValueError("Video dimensions must be positive")

    scale = 1.0
    if profile.max_width is not None:
        scale = min(scale, profile.max_width / width)
    if profile.max_height is not None:
        scale = min(scale, profile.max_height / height)

    output_width = max(2, int(width * scale) // 2 * 2)
    output_height = max(2, int(height * scale) // 2 * 2)
    if profile.max_width is not None:
        output_width = min(output_width, profile.max_width // 2 * 2)
    if profile.max_height is not None:
        output_height = min(output_height, profile.max_height // 2 * 2)
    return output_width, output_height


def build_video_encoder_command(
    ffmpeg,
    output_path,
    input_width,
    input_height,
    profile,
):
    output_width, output_height = calculate_output_size(
        input_width,
        input_height,
        profile,
    )
    command = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pixel_format",
        "bgr24",
        "-video_size",
        f"{input_width}x{input_height}",
        "-framerate",
        str(profile.fps),
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
    ]
    if (output_width, output_height) != (input_width, input_height):
        command.extend(
            [
                "-vf",
                f"scale={output_width}:{output_height}:flags=fast_bilinear",
            ]
        )
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            profile.preset,
            "-crf",
            str(profile.crf),
            "-pix_fmt",
            "yuv420p",
            "-f",
            "matroska",
            str(output_path),
        ]
    )
    return command


@dataclass
class _FramePacket:
    frame: np.ndarray
    repeat_count: int


class BoundedFrameBuffer:
    """Non-blocking bounded buffer that preserves timeline frame counts."""

    def __init__(self, capacity=3):
        if capacity <= 0:
            raise ValueError("Frame buffer capacity must be positive")
        self.capacity = int(capacity)
        self._items = collections.deque()
        self._closed = False
        self._condition = threading.Condition()
        self.dropped_visual_frames = 0
        self.max_observed_size = 0

    def submit(self, frame, repeat_count=1):
        repeat_count = int(repeat_count)
        if repeat_count <= 0:
            return
        with self._condition:
            if self._closed:
                raise FFmpegVideoError("Video writer is already closed")
            if len(self._items) >= self.capacity:
                replaced = self._items.pop()
                repeat_count += replaced.repeat_count
                self.dropped_visual_frames += replaced.repeat_count
            self._items.append(_FramePacket(frame, repeat_count))
            self.max_observed_size = max(
                self.max_observed_size,
                len(self._items),
            )
            self._condition.notify()

    def take(self):
        with self._condition:
            while not self._items and not self._closed:
                self._condition.wait()
            if self._items:
                return self._items.popleft()
            return None

    def close(self, discard=False):
        with self._condition:
            self._closed = True
            if discard:
                self._items.clear()
            self._condition.notify_all()

    @property
    def pending_count(self):
        with self._condition:
            return len(self._items)


class FFmpegVideoWriter:
    def __init__(
        self,
        output_path,
        input_width,
        input_height,
        profile,
        queue_size=3,
        logger=None,
        process_factory=None,
    ):
        self.output_path = Path(output_path)
        self.input_width = int(input_width)
        self.input_height = int(input_height)
        self.profile = profile
        self.logger = logger or (lambda message: None)
        self.buffer = BoundedFrameBuffer(queue_size)
        self.diagnostic_log_path = self.output_path.with_suffix(
            ".ffmpeg.log"
        )
        self.stderr_tail = collections.deque(maxlen=200)
        self.written_frames = 0
        self.submitted_frames = 0
        self._error = None
        self._closed = False

        try:
            ffmpeg = find_ffmpeg()
        except MediaMuxError as exc:
            raise FFmpegVideoError(str(exc)) from exc
        self.command = build_video_encoder_command(
            ffmpeg,
            self.output_path,
            self.input_width,
            self.input_height,
            profile,
        )
        self.logger(f"Streaming FFmpeg started: {' '.join(self.command)}")

        factory = process_factory or subprocess.Popen
        process_kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
        }
        if sys.platform == "win32":
            process_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.process = factory(self.command, **process_kwargs)
        except OSError as exc:
            raise FFmpegVideoError(
                f"Failed to start FFmpeg video encoder: {exc}"
            ) from exc

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="ffmpeg-video-stderr",
            daemon=True,
        )
        self._writer_thread = threading.Thread(
            target=self._write_frames,
            name="ffmpeg-video-stdin",
            daemon=True,
        )
        self._stderr_thread.start()
        self._writer_thread.start()

    def submit(self, frame, repeat_count=1):
        if self._error is not None:
            raise FFmpegVideoError(self._diagnostic_error_message())
        if frame.shape != (
            self.input_height,
            self.input_width,
            3,
        ):
            raise FFmpegVideoError(
                "Unexpected frame shape: "
                f"{frame.shape}, expected "
                f"({self.input_height}, {self.input_width}, 3)"
            )
        contiguous = np.ascontiguousarray(frame, dtype=np.uint8)
        self.buffer.submit(contiguous, repeat_count)
        self.submitted_frames += int(repeat_count)

    def _write_frames(self):
        try:
            while True:
                packet = self.buffer.take()
                if packet is None:
                    break
                frame_bytes = packet.frame.tobytes()
                for _ in range(packet.repeat_count):
                    self.process.stdin.write(frame_bytes)
                    self.written_frames += 1
        except (BrokenPipeError, OSError, ValueError) as exc:
            self._error = FFmpegVideoError(
                f"FFmpeg stopped while receiving video frames: {exc}"
            )
            self.buffer.close(discard=True)
        finally:
            try:
                self.process.stdin.close()
            except (AttributeError, OSError, ValueError):
                pass

    def _drain_stderr(self):
        try:
            with self.diagnostic_log_path.open(
                "w",
                encoding="utf-8",
                errors="replace",
            ) as log_file:
                while True:
                    line = self.process.stderr.readline()
                    if not line:
                        break
                    if isinstance(line, bytes):
                        line = line.decode(errors="replace")
                    self.stderr_tail.append(line.rstrip())
                    log_file.write(line)
                    log_file.flush()
        except (AttributeError, OSError, ValueError) as exc:
            self.stderr_tail.append(f"stderr reader failed: {exc}")
        finally:
            try:
                self.process.stderr.close()
            except (AttributeError, OSError, ValueError):
                pass

    def _stop_process(self):
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def _diagnostic_error_message(self):
        return (
            f"{self._error}. Diagnostic log: "
            f"{self.diagnostic_log_path}"
        )

    def close(self, timeout=60):
        if self._closed:
            if self._error is not None:
                raise FFmpegVideoError(self._diagnostic_error_message())
            return
        self._closed = True
        self.buffer.close()

        started_at = time.monotonic()
        self._writer_thread.join(timeout)
        if self._writer_thread.is_alive():
            self._stop_process()
            self.buffer.close(discard=True)
            self._writer_thread.join(5)
            self._error = FFmpegVideoError(
                "FFmpeg video encoder did not finish in time"
            )

        remaining = max(1.0, timeout - (time.monotonic() - started_at))
        try:
            return_code = self.process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            self._stop_process()
            return_code = self.process.returncode
            self._error = FFmpegVideoError(
                "FFmpeg video encoder did not exit in time"
            )
        self._stderr_thread.join(5)

        if return_code != 0 and self._error is None:
            details = "\n".join(self.stderr_tail)
            self._error = FFmpegVideoError(
                f"FFmpeg video encoder failed with code {return_code}: "
                f"{details}"
            )
        if not self.output_path.is_file() and self._error is None:
            self._error = FFmpegVideoError(
                f"FFmpeg did not create video file: {self.output_path}"
            )
        if (
            self.written_frames != self.submitted_frames
            and self._error is None
        ):
            self._error = FFmpegVideoError(
                "FFmpeg video frame count mismatch: "
                f"submitted={self.submitted_frames}, "
                f"written={self.written_frames}"
            )
        if self._error is not None:
            raise FFmpegVideoError(self._diagnostic_error_message())

        try:
            self.diagnostic_log_path.unlink()
        except FileNotFoundError:
            pass
        self.logger(
            "Streaming FFmpeg completed: "
            f"frames={self.written_frames} "
            f"dropped_visual_frames={self.buffer.dropped_visual_frames}"
        )

    def abort(self):
        self._closed = True
        self.buffer.close(discard=True)
        self._stop_process()
        self._writer_thread.join(5)
        self._stderr_thread.join(5)
