import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from audio_capture import (
    AUDIO_MICROPHONE,
    AUDIO_SYSTEM,
    AudioTrack,
    apply_microphone_gain,
)
from media_mux import (
    MIX_LIMIT,
    MediaMuxError,
    build_mixed_audio_filter,
    build_mux_command,
    find_ffmpeg,
    mux_recording,
)


def make_track(source, path):
    return AudioTrack(
        source=source,
        path=path,
        sample_rate=48000,
        channels=2 if source == AUDIO_SYSTEM else 1,
        duration=1.0,
        blocks_received=10,
        blocks_lost=0,
    )


class MediaMuxTests(unittest.TestCase):
    def test_frozen_build_uses_bundled_ffmpeg_before_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundled = Path(temp_dir) / "ffmpeg.exe"
            bundled.write_bytes(b"bundled")

            with patch("media_mux.sys._MEIPASS", temp_dir, create=True), patch(
                "media_mux.shutil.which", return_value=r"C:\\ffmpeg\\ffmpeg.exe"
            ) as which_mock:
                self.assertEqual(find_ffmpeg(), str(bundled))

            which_mock.assert_not_called()

    def test_frozen_build_does_not_fall_back_to_system_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("media_mux.sys._MEIPASS", temp_dir, create=True), patch(
                "media_mux.shutil.which", return_value=r"C:\\ffmpeg\\ffmpeg.exe"
            ) as which_mock:
                with self.assertRaisesRegex(MediaMuxError, "Bundled FFmpeg was not found"):
                    find_ffmpeg()

            which_mock.assert_not_called()

    def test_development_build_can_use_path_ffmpeg(self):
        with patch("media_mux.sys._MEIPASS", None, create=True), patch(
            "media_mux.Path.is_file", return_value=False
        ), patch(
            "media_mux.shutil.which", return_value=r"C:\\ffmpeg\\ffmpeg.exe"
        ):
            self.assertEqual(find_ffmpeg(), r"C:\\ffmpeg\\ffmpeg.exe")

    def test_video_only_mux_has_no_audio_output(self):
        command = build_mux_command(
            "ffmpeg.exe",
            "raw.mkv",
            "final.mp4",
            [],
        )

        self.assertIn("-an", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertNotIn("libx264", command)
        self.assertIn("+faststart", command)
        self.assertNotIn("aac", command)

    def test_microphone_mux_uses_aac_and_explicit_output_format(self):
        track = make_track(AUDIO_MICROPHONE, Path("microphone.wav"))
        command = build_mux_command(
            "ffmpeg.exe",
            "raw.mkv",
            "final.mp4",
            [track],
            output_duration=1.25,
        )

        self.assertIn("aac", command)
        self.assertIn("160k", command)
        self.assertIn("48000", command)
        self.assertIn("2", command)
        self.assertIn("-shortest", command)
        self.assertIn("+faststart", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-t") + 1], "1.250000")
        filter_graph = command[command.index("-filter_complex") + 1]
        self.assertIn("aresample=48000", filter_graph)
        self.assertIn("channel_layouts=stereo", filter_graph)
        self.assertIn("apad", filter_graph)

    def test_mixed_mux_uses_independent_gains_without_amix_attenuation(self):
        tracks = [
            make_track(AUDIO_SYSTEM, Path("system.wav")),
            make_track(AUDIO_MICROPHONE, Path("microphone.wav")),
        ]
        command = build_mux_command(
            "ffmpeg.exe",
            "raw.mkv",
            "final.mp4",
            tracks,
        )
        filter_graph = command[command.index("-filter_complex") + 1]

        self.assertIn("[1:a]", filter_graph)
        self.assertIn("[2:a]", filter_graph)
        self.assertIn("volume=-3.0dB[system]", filter_graph)
        self.assertIn("volume=0.0dB[microphone]", filter_graph)
        self.assertIn("amix=inputs=2", filter_graph)
        self.assertIn("normalize=0", filter_graph)
        self.assertIn("alimiter=limit=0.95", filter_graph)

    def test_mux_invokes_ffmpeg_and_requires_output_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "raw.mkv"
            final_path = Path(temp_dir) / "final.mp4"
            raw_path.write_bytes(b"raw")

            def fake_run(command, **kwargs):
                final_path.write_bytes(b"muxed")
                return subprocess.CompletedProcess(command, 0, b"", b"")

            with patch("media_mux.find_ffmpeg", return_value="ffmpeg.exe"), patch(
                "media_mux.subprocess.run", side_effect=fake_run
            ) as run_mock:
                mux_recording(raw_path, final_path, [])

            self.assertTrue(final_path.exists())
            run_mock.assert_called_once()

    def test_real_mix_contains_both_sources_and_does_not_clip(self):
        ffmpeg = find_ffmpeg()
        sample_rate = 48000
        duration = 0.25
        frame_count = round(sample_rate * duration)
        time_axis = np.arange(frame_count) / sample_rate
        system = 0.9 * np.sin(2 * np.pi * 440 * time_axis)
        raw_microphone = 0.08 * np.sin(2 * np.pi * 880 * time_axis)
        microphone_pcm = apply_microphone_gain(
            np.rint(raw_microphone * 32767).astype("<i2").tobytes()
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            system_path = Path(temp_dir) / "system.wav"
            microphone_path = Path(temp_dir) / "microphone.wav"
            with wave.open(str(system_path), "wb") as wav_file:
                wav_file.setnchannels(2)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                stereo_system = np.column_stack((system, system))
                wav_file.writeframes(
                    np.rint(stereo_system * 32767).astype("<i2").tobytes()
                )
            with wave.open(str(microphone_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(microphone_pcm)

            filter_graph = build_mixed_audio_filter(0, 1)
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(system_path),
                    "-i",
                    str(microphone_path),
                    "-filter_complex",
                    filter_graph,
                    "-map",
                    "[aout]",
                    "-t",
                    str(duration),
                    "-f",
                    "f32le",
                    "-acodec",
                    "pcm_f32le",
                    "pipe:1",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )

        mixed = np.frombuffer(result.stdout, dtype="<f4").reshape(-1, 2)[:, 0]
        spectrum = np.abs(np.fft.rfft(mixed))
        frequencies = np.fft.rfftfreq(mixed.size, 1 / sample_rate)
        system_bin = np.argmin(np.abs(frequencies - 440))
        microphone_bin = np.argmin(np.abs(frequencies - 880))
        self.assertGreater(spectrum[system_bin], 100)
        self.assertGreater(spectrum[microphone_bin], 100)
        self.assertLessEqual(float(np.max(np.abs(mixed))), MIX_LIMIT + 0.002)
        jumps = np.abs(np.diff(mixed))
        boundary_indexes = np.arange(1024, mixed.size, 1024) - 1
        interior_mask = np.ones(jumps.size, dtype=bool)
        interior_mask[boundary_indexes] = False
        self.assertLessEqual(
            np.percentile(jumps[boundary_indexes], 99),
            np.percentile(jumps[interior_mask], 99) * 2,
        )

    def test_quiet_microphone_is_amplified_without_pcm_clipping(self):
        raw = np.full(4800, 300, dtype="<i2")

        processed = np.frombuffer(
            apply_microphone_gain(raw.tobytes()),
            dtype="<i2",
        )

        self.assertGreater(float(np.mean(processed)), 1500)
        self.assertLessEqual(
            int(np.max(np.abs(processed))),
            round(32767 * 0.95),
        )


if __name__ == "__main__":
    unittest.main()
