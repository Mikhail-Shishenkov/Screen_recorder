import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ffmpeg_video_writer import (
    BoundedFrameBuffer,
    FFmpegVideoError,
    FFmpegVideoWriter,
    VIDEO_PROFILES,
    build_video_encoder_command,
    calculate_output_size,
    get_video_profile,
)
from media_mux import find_ffmpeg, mux_recording


class _FakeStdin(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.bytes_written = 0

    def write(self, data):
        self.bytes_written += len(data)
        return len(data)


class _FakeProcess:
    def __init__(self, return_code=0, stderr=b""):
        self.stdin = _FakeStdin()
        self.stderr = io.BytesIO(stderr)
        self.returncode = return_code
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class VideoProfileTests(unittest.TestCase):
    def test_encoder_commands_for_all_modes(self):
        expected = {
            "tracker": (15, 28),
            "maximum": (24, 21),
            "compact": (15, 28),
        }
        for profile in VIDEO_PROFILES:
            with self.subTest(profile=profile.key):
                command = build_video_encoder_command(
                    "ffmpeg.exe",
                    "output.mkv",
                    3840,
                    2160,
                    profile,
                )
                fps, crf = expected[profile.key]
                self.assertEqual(
                    command[command.index("-framerate") + 1],
                    str(fps),
                )
                self.assertEqual(
                    command[command.index("-crf") + 1],
                    str(crf),
                )
                self.assertEqual(
                    command[command.index("-preset") + 1],
                    "veryfast",
                )
                self.assertIn("libx264", command)
                self.assertIn("yuv420p", command)
                self.assertEqual(command[-2], "matroska")

    def test_native_modes_do_not_scale_4k(self):
        for key in ("tracker", "maximum"):
            command = build_video_encoder_command(
                "ffmpeg.exe",
                "output.mkv",
                3840,
                2160,
                get_video_profile(key),
            )
            self.assertNotIn("-vf", command)
            self.assertEqual(
                calculate_output_size(
                    3840,
                    2160,
                    get_video_profile(key),
                ),
                (3840, 2160),
            )

    def test_compact_mode_limits_4k_to_1080p(self):
        profile = get_video_profile("compact")
        self.assertEqual(
            calculate_output_size(3840, 2160, profile),
            (1920, 1080),
        )
        command = build_video_encoder_command(
            "ffmpeg.exe",
            "output.mkv",
            3840,
            2160,
            profile,
        )
        self.assertIn("-vf", command)
        self.assertIn(
            "scale=1920:1080:flags=fast_bilinear",
            command,
        )

    def test_compact_mode_does_not_enlarge_small_input(self):
        self.assertEqual(
            calculate_output_size(
                1280,
                720,
                get_video_profile("compact"),
            ),
            (1280, 720),
        )

    def test_compact_mode_rounds_odd_input_down_without_enlarging(self):
        width, height = calculate_output_size(
            1279,
            719,
            get_video_profile("compact"),
        )
        self.assertEqual((width, height), (1278, 718))

    def test_compact_mode_preserves_ratio_and_even_dimensions(self):
        width, height = calculate_output_size(
            3440,
            1440,
            get_video_profile("compact"),
        )
        self.assertLessEqual(width, 1920)
        self.assertLessEqual(height, 1080)
        self.assertEqual(width % 2, 0)
        self.assertEqual(height % 2, 0)
        self.assertLess(abs(width / height - 3440 / 1440), 0.01)


class FrameBufferTests(unittest.TestCase):
    def test_buffer_is_bounded_and_preserves_timeline_count(self):
        buffer = BoundedFrameBuffer(capacity=2)
        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        for _ in range(10):
            buffer.submit(frame)

        self.assertEqual(buffer.pending_count, 2)
        self.assertEqual(buffer.max_observed_size, 2)
        buffer.close()
        total_frames = 0
        while True:
            packet = buffer.take()
            if packet is None:
                break
            total_frames += packet.repeat_count
        self.assertEqual(total_frames, 10)
        self.assertGreater(buffer.dropped_visual_frames, 0)


class FFmpegWriterTests(unittest.TestCase):
    def test_writer_closes_stdin_and_process(self):
        process = _FakeProcess()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "video.mkv"
            with patch(
                "ffmpeg_video_writer.find_ffmpeg",
                return_value="ffmpeg.exe",
            ):
                writer = FFmpegVideoWriter(
                    output,
                    4,
                    2,
                    get_video_profile("tracker"),
                    process_factory=lambda *args, **kwargs: process,
                )
                writer.submit(
                    np.zeros((2, 4, 3), dtype=np.uint8),
                    repeat_count=3,
                )
                output.write_bytes(b"mkv")
                writer.close()

            self.assertEqual(process.stdin.bytes_written, 4 * 2 * 3 * 3)
            self.assertTrue(process.stdin.closed)
            self.assertEqual(writer.written_frames, 3)
            self.assertFalse(writer.diagnostic_log_path.exists())

    def test_writer_error_keeps_diagnostic_log(self):
        process = _FakeProcess(
            return_code=1,
            stderr=b"encoder failed\n",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "video.mkv"
            with patch(
                "ffmpeg_video_writer.find_ffmpeg",
                return_value="ffmpeg.exe",
            ):
                writer = FFmpegVideoWriter(
                    output,
                    4,
                    2,
                    get_video_profile("tracker"),
                    process_factory=lambda *args, **kwargs: process,
                )
                with self.assertRaisesRegex(
                    FFmpegVideoError,
                    r"failed with code 1.*Diagnostic log:",
                ):
                    writer.close()

            self.assertTrue(writer.diagnostic_log_path.exists())
            self.assertIn(
                "encoder failed",
                writer.diagnostic_log_path.read_text(encoding="utf-8"),
            )

    def test_abort_stops_process_and_worker_threads(self):
        process = _FakeProcess(return_code=None)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "video.mkv"
            with patch(
                "ffmpeg_video_writer.find_ffmpeg",
                return_value="ffmpeg.exe",
            ):
                writer = FFmpegVideoWriter(
                    output,
                    4,
                    2,
                    get_video_profile("tracker"),
                    process_factory=lambda *args, **kwargs: process,
                )
                writer.abort()

            self.assertTrue(process.terminated)
            self.assertFalse(writer._writer_thread.is_alive())
            self.assertFalse(writer._stderr_thread.is_alive())

    def test_real_ffmpeg_encodes_h264_and_muxes_with_stream_copy(self):
        ffmpeg = find_ffmpeg()
        profile = get_video_profile("tracker")
        with tempfile.TemporaryDirectory() as temp_dir:
            mkv_path = Path(temp_dir) / "video.mkv"
            mp4_path = Path(temp_dir) / "video.mp4"
            writer = FFmpegVideoWriter(
                mkv_path,
                64,
                48,
                profile,
            )
            for index in range(8):
                frame = np.full(
                    (48, 64, 3),
                    index * 20,
                    dtype=np.uint8,
                )
                writer.submit(frame)
            writer.close()

            mux_recording(
                mkv_path,
                mp4_path,
                [],
                output_duration=8 / profile.fps,
            )
            probe = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-i",
                    str(mp4_path),
                    "-f",
                    "null",
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            details = probe.stderr.decode(errors="replace")
            self.assertEqual(probe.returncode, 0, details)
            self.assertIn("Video: h264", details)
            self.assertIn("yuv420p", details)
            self.assertTrue(mp4_path.is_file())


if __name__ == "__main__":
    unittest.main()
