import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from screen_recorder import RecorderThread


def run_recording(output_dir, width, height, fps, duration):
    raw_path = output_dir / f"smoke_{width}x{height}_{fps}fps_raw.mp4"
    final_path = output_dir / f"smoke_{width}x{height}_{fps}fps.mp4"
    worker = RecorderThread((0, 0, width, height), str(raw_path), fps)
    worker.start()

    startup_deadline = time.perf_counter() + 10.0
    while worker.captured_frame_count == 0 and worker.isRunning():
        if time.perf_counter() >= startup_deadline:
            worker.is_recording = False
            worker.wait()
            raise RuntimeError("capture did not start within 10 seconds")
        time.sleep(0.01)

    time.sleep(duration)
    worker.is_recording = False
    if not worker.wait(30000):
        raise RuntimeError("recorder worker did not stop within 30 seconds")

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg was not found")
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(raw_path),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(final_path),
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    raw_path.unlink()

    return {
        "size": f"{width}x{height}",
        "fps": fps,
        "captured_frames": worker.captured_frame_count,
        "output_frames": worker.frame_count,
        "missed_frames": worker.missed_frame_count,
        "active_seconds": worker.active_recording_seconds,
        "capture_fps": (
            worker.captured_frame_count / worker.active_recording_seconds
            if worker.active_recording_seconds
            else 0.0
        ),
        "output_path": str(final_path),
        "file_bytes": final_path.stat().st_size,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("benchmark_output/smoke")
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for width, height in ((1280, 720), (1920, 1080)):
        for fps in (15, 24, 30):
            print(f"RUN size={width}x{height} fps={fps}", flush=True)
            result = run_recording(
                args.output_dir, width, height, fps, args.duration
            )
            results.append(result)
            print(
                f"RESULT capture_fps={result['capture_fps']:.2f} "
                f"captured={result['captured_frames']} "
                f"output={result['output_frames']} "
                f"missed={result['missed_frames']} "
                f"active={result['active_seconds']:.3f}s "
                f"bytes={result['file_bytes']}",
                flush=True,
            )

    results_path = args.output_dir / "results.json"
    results_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"JSON {results_path}", flush=True)


if __name__ == "__main__":
    main()
