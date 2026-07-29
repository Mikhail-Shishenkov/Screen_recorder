import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ffmpeg_video_writer import VIDEO_PROFILES_BY_KEY, calculate_output_size
from media_mux import find_ffmpeg, mux_recording
from screen_recorder import RecorderThread


def inspect_video(path):
    result = subprocess.run(
        [
            find_ffmpeg(),
            "-hide_banner",
            "-i",
            str(path),
            "-f",
            "null",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    details = result.stderr.decode(errors="replace")
    video_match = re.search(
        r"Video:\s*([^,]+),\s*([^,(]+).*?(\d+)x(\d+).*?([\d.]+)\s*fps",
        details,
    )
    duration_match = re.search(
        r"Duration:\s*(\d+):(\d+):([\d.]+)",
        details,
    )
    duration = None
    if duration_match:
        duration = (
            int(duration_match.group(1)) * 3600
            + int(duration_match.group(2)) * 60
            + float(duration_match.group(3))
        )
    return {
        "return_code": result.returncode,
        "codec": video_match.group(1).strip() if video_match else None,
        "pixel_format": video_match.group(2).strip() if video_match else None,
        "width": int(video_match.group(3)) if video_match else None,
        "height": int(video_match.group(4)) if video_match else None,
        "fps": float(video_match.group(5)) if video_match else None,
        "duration": duration,
    }


def run_recording(output_dir, width, height, profile_key, duration):
    profile = VIDEO_PROFILES_BY_KEY[profile_key]
    raw_path = output_dir / (
        f"smoke_{profile_key}_{width}x{height}_video.mkv"
    )
    final_path = output_dir / f"smoke_{profile_key}_{width}x{height}.mp4"
    worker = RecorderThread(
        (0, 0, width, height),
        str(raw_path),
        profile_key,
    )
    worker.start()

    startup_deadline = time.perf_counter() + 15.0
    while worker.captured_frame_count == 0 and worker.isRunning():
        if time.perf_counter() >= startup_deadline:
            worker.is_recording = False
            worker.wait()
            raise RuntimeError("capture did not start within 15 seconds")
        time.sleep(0.01)

    time.sleep(duration)
    worker.is_recording = False
    if not worker.wait(120000):
        raise RuntimeError("recorder worker did not stop within 120 seconds")
    if not raw_path.is_file():
        raise RuntimeError("streaming encoder did not create MKV")

    raw_bytes = raw_path.stat().st_size
    finalization_started = time.perf_counter()
    mux_recording(
        raw_path,
        final_path,
        [],
        output_duration=worker.frame_count / worker.effective_fps,
    )
    finalization_seconds = time.perf_counter() - finalization_started
    final_bytes = final_path.stat().st_size
    raw_path.unlink()

    expected_width, expected_height = calculate_output_size(
        width,
        height,
        profile,
    )
    return {
        "profile": profile_key,
        "capture_size": f"{width}x{height}",
        "expected_output_size": f"{expected_width}x{expected_height}",
        "target_fps": profile.fps,
        "crf": profile.crf,
        "preset": profile.preset,
        "captured_frames": worker.captured_frame_count,
        "output_frames": worker.frame_count,
        "missed_frames": worker.missed_frame_count,
        "active_seconds": worker.active_recording_seconds,
        "timeline_seconds": worker.frame_count / worker.effective_fps,
        "capture_fps": (
            worker.captured_frame_count / worker.active_recording_seconds
            if worker.active_recording_seconds
            else 0.0
        ),
        "finalization_seconds": finalization_seconds,
        "temporary_bytes": raw_bytes,
        "final_bytes": final_bytes,
        "output_path": str(final_path),
        "media": inspect_video(final_path),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument(
        "--profiles",
        nargs="+",
        choices=tuple(VIDEO_PROFILES_BY_KEY),
        default=tuple(VIDEO_PROFILES_BY_KEY),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark_output/streaming_smoke"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for profile_key in args.profiles:
        print(
            f"RUN profile={profile_key} "
            f"size={args.width}x{args.height}",
            flush=True,
        )
        result = run_recording(
            args.output_dir,
            args.width,
            args.height,
            profile_key,
            args.duration,
        )
        results.append(result)
        print(
            "RESULT "
            + json.dumps(result, ensure_ascii=False),
            flush=True,
        )

    results_path = args.output_dir / "results.json"
    results_path.write_text(
        json.dumps(results, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"JSON {results_path}", flush=True)


if __name__ == "__main__":
    main()
