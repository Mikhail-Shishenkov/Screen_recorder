import argparse
import audioop
import json
import math
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import pyaudiowpatch as pyaudio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio_capture import (
    AUDIO_MICROPHONE,
    AUDIO_MIXED,
    AUDIO_OFF,
    AUDIO_SYSTEM,
    AudioSession,
)
from media_mux import mux_recording
from screen_recorder import RecorderThread


class TonePlayer(threading.Thread):
    def __init__(
        self,
        duration,
        frequency=440.0,
        sample_rate=48000,
        amplitude=5000,
    ):
        super().__init__(name="audio-smoke-tone")
        self.duration = duration
        self.frequency = frequency
        self.sample_rate = sample_rate
        self.amplitude = amplitude
        self.error = None

    def run(self):
        audio = pyaudio.PyAudio()
        stream = None
        try:
            device = audio.get_default_wasapi_device(d_out=True)
            stream = audio.open(
                format=pyaudio.paInt16,
                channels=2,
                rate=self.sample_rate,
                output=True,
                output_device_index=int(device["index"]),
                frames_per_buffer=1024,
            )
            total_frames = int(self.duration * self.sample_rate)
            for offset in range(0, total_frames, 1024):
                count = min(1024, total_frames - offset)
                samples = []
                for index in range(count):
                    position = offset + index
                    value = int(
                        self.amplitude
                        * math.sin(
                            2.0
                            * math.pi
                            * self.frequency
                            * position
                            / self.sample_rate
                        )
                    )
                    samples.append(struct.pack("<hh", value, value))
                stream.write(b"".join(samples))
        except Exception as exc:
            self.error = exc
        finally:
            if stream is not None:
                stream.stop_stream()
                stream.close()
            audio.terminate()


def wav_metrics(path):
    with wave.open(str(path), "rb") as wav_file:
        data = wav_file.readframes(wav_file.getnframes())
        return {
            "duration": wav_file.getnframes() / wav_file.getframerate(),
            "sample_rate": wav_file.getframerate(),
            "channels": wav_file.getnchannels(),
            "rms": audioop.rms(data, wav_file.getsampwidth()) if data else 0,
        }


def run_mode(
    output_dir,
    mode,
    active_duration,
    system_amplitude=5000,
    diagnostics=False,
    tone_microphone=False,
):
    raw_path = output_dir / f"audio_{mode}_raw.mp4"
    final_path = output_dir / f"audio_{mode}.mp4"
    audio_prefix = output_dir / f"audio_{mode}"
    session = AudioSession(
        mode,
        audio_prefix,
        logger=print if diagnostics else None,
        diagnostics=diagnostics,
    )
    recorder = RecorderThread((0, 0, 640, 480), str(raw_path), 15)
    tone = None

    session.start()
    recorder.start()
    if mode in (AUDIO_SYSTEM, AUDIO_MIXED) or tone_microphone:
        tone = TonePlayer(
            active_duration + 1.0,
            amplitude=system_amplitude,
        )
        tone.start()

    deadline = time.perf_counter() + 10.0
    while recorder.captured_frame_count == 0:
        if time.perf_counter() >= deadline:
            raise RuntimeError("video capture did not start")
        time.sleep(0.01)

    first_part = active_duration / 2.0
    time.sleep(first_part)
    if mode == AUDIO_MIXED:
        recorder.is_paused = True
        session.set_paused(True)
        time.sleep(0.5)
        recorder.is_paused = False
        session.set_paused(False)
    time.sleep(active_duration - first_part)

    session.request_stop()
    recorder.is_recording = False
    if not recorder.wait(30000):
        raise RuntimeError("video recorder did not stop")
    tracks = session.stop()
    if tone is not None:
        tone.join()
        if tone.error is not None:
            raise tone.error

    track_metrics = {
        track.source: {
            **wav_metrics(track.path),
            "device_index": track.device_index,
            "device_name": track.device_name,
            "backend": track.backend,
            "sample_format": track.sample_format,
            "gain_db": track.gain_db,
            "raw_rms_dbfs": track.raw_rms_dbfs,
            "raw_peak_dbfs": track.raw_peak_dbfs,
            "processed_rms_dbfs": track.processed_rms_dbfs,
            "processed_peak_dbfs": track.processed_peak_dbfs,
        }
        for track in tracks
    }
    mux_recording(
        raw_path,
        final_path,
        tracks,
        output_duration=recorder.frame_count / recorder.effective_fps,
        logger=print if diagnostics else None,
        diagnostics=diagnostics,
    )
    raw_path.unlink()
    session.cleanup()

    return {
        "mode": mode,
        "video_active_seconds": recorder.active_recording_seconds,
        "video_frames": recorder.frame_count,
        "tracks": track_metrics,
        "output_path": str(final_path),
        "file_bytes": final_path.stat().st_size,
        "system_amplitude": system_amplitude,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=1.5)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark_output/audio_smoke"),
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=(AUDIO_OFF, AUDIO_SYSTEM, AUDIO_MICROPHONE, AUDIO_MIXED),
        default=(AUDIO_OFF, AUDIO_SYSTEM, AUDIO_MICROPHONE, AUDIO_MIXED),
    )
    parser.add_argument("--system-amplitude", type=int, default=5000)
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--tone-microphone", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for mode in args.modes:
        print(f"RUN mode={mode}", flush=True)
        result = run_mode(
            args.output_dir,
            mode,
            args.duration,
            system_amplitude=args.system_amplitude,
            diagnostics=args.diagnostics,
            tone_microphone=args.tone_microphone,
        )
        results.append(result)
        print(
            f"RESULT mode={mode} video={result['video_active_seconds']:.3f}s "
            f"tracks={json.dumps(result['tracks'], ensure_ascii=True)} "
            f"bytes={result['file_bytes']}",
            flush=True,
        )

    result_path = args.output_dir / "results.json"
    result_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"JSON {result_path}", flush=True)


if __name__ == "__main__":
    main()
