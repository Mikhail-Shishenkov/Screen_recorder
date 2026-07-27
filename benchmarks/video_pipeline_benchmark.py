import argparse
import ctypes
import json
import math
import queue
import statistics
import sys
import threading
import time
from pathlib import Path

import cv2
import mss
import numpy as np
import pyautogui
from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from frame_scheduler import FrameScheduler


def percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def summarize(values):
    if not values:
        return {"avg_ms": 0.0, "min_ms": 0.0, "p95_ms": 0.0}
    milliseconds = [value * 1000.0 for value in values]
    return {
        "avg_ms": statistics.fmean(milliseconds),
        "min_ms": min(milliseconds),
        "p95_ms": percentile(milliseconds, 0.95),
    }


def capture_imagegrab(bbox):
    started = time.perf_counter()
    image = ImageGrab.grab(bbox=bbox, all_screens=True)
    captured = time.perf_counter()
    frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
    converted = time.perf_counter()
    return frame, captured - started, converted - captured


def capture_mss(session, monitor):
    started = time.perf_counter()
    image = session.grab(monitor)
    captured = time.perf_counter()
    frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_BGRA2BGR)
    converted = time.perf_counter()
    return frame, captured - started, converted - captured


def draw_pointer_and_clicks(frame, origin, click_state):
    state_started = time.perf_counter()
    mouse_x, mouse_y = pyautogui.position()
    user32 = ctypes.windll.user32
    left_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
    right_down = bool(user32.GetAsyncKeyState(0x02) & 0x8000)
    state_finished = time.perf_counter()

    if left_down and not click_state["left_down"]:
        click_state["left_anim"] = 10
    if right_down and not click_state["right_down"]:
        click_state["right_anim"] = 10
    click_state["left_down"] = left_down
    click_state["right_down"] = right_down

    draw_started = time.perf_counter()
    x = mouse_x - origin[0]
    y = mouse_y - origin[1]
    height, width = frame.shape[:2]
    if 0 <= x < width and 0 <= y < height:
        cx, cy = int(x), int(y)
        points = np.array(
            [[cx, cy], [cx + 10, cy + 25], [cx + 4, cy + 18], [cx - 6, cy + 22]],
            np.int32,
        )
        cv2.fillConvexPoly(frame, points, (255, 255, 255))
        cv2.polylines(frame, [points], True, (0, 0, 0), 1)
        if click_state["left_anim"] > 0:
            radius = 20 + (10 - click_state["left_anim"]) * 2
            cv2.circle(frame, (cx, cy), radius, (0, 0, 255), 2)
            click_state["left_anim"] -= 1
        if click_state["right_anim"] > 0:
            radius = 20 + (10 - click_state["right_anim"]) * 2
            cv2.circle(frame, (cx, cy), radius, (255, 0, 0), 2)
            click_state["right_anim"] -= 1
    draw_finished = time.perf_counter()
    return state_finished - state_started, draw_finished - draw_started


def run_case(backend, scenario, width, height, fps, duration, output_dir, schedule_mode):
    bbox = (0, 0, width, height)
    monitor = {"left": 0, "top": 0, "width": width, "height": height}
    writer = None
    writer_queue = None
    writer_thread = None
    writer_error = []
    output_path = None
    session = mss.mss() if backend == "mss" else None
    timings = {
        "capture": [],
        "convert": [],
        "cursor_state": [],
        "draw": [],
        "write": [],
        "wait": [],
        "process": [],
        "cycle": [],
    }
    click_state = {
        "left_down": False,
        "right_down": False,
        "left_anim": 0,
        "right_anim": 0,
    }
    frame_period = 1.0 / fps
    frame_count = 0
    output_frames = 0
    started = time.perf_counter()
    scheduler = FrameScheduler(fps, started) if schedule_mode == "absolute" else None

    try:
        while time.perf_counter() - started < duration:
            if scheduler is not None:
                wait_time = scheduler.wait_seconds(time.perf_counter())
                if wait_time:
                    wait_started = time.perf_counter()
                    time.sleep(wait_time)
                    timings["wait"].append(time.perf_counter() - wait_started)

            cycle_started = time.perf_counter()
            if backend == "imagegrab":
                frame, capture_time, convert_time = capture_imagegrab(bbox)
            else:
                frame, capture_time, convert_time = capture_mss(session, monitor)
            timings["capture"].append(capture_time)
            timings["convert"].append(convert_time)

            cursor_time = 0.0
            draw_time = 0.0
            if scenario in ("overlay", "encode", "encode_async"):
                cursor_time, draw_time = draw_pointer_and_clicks(
                    frame, (bbox[0], bbox[1]), click_state
                )
            timings["cursor_state"].append(cursor_time)
            timings["draw"].append(draw_time)

            frame_count += 1
            due_frames = 1
            if scheduler is not None:
                scheduler.mark_captured()
                due_frames = scheduler.claim_due_frames(time.perf_counter())

            write_time = 0.0
            if scenario in ("encode", "encode_async"):
                if writer is None:
                    output_path = output_dir / (
                        f"{backend}_{width}x{height}_{fps}fps.mp4"
                    )
                    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                    writer = cv2.VideoWriter(
                        str(output_path), fourcc, float(fps), (width, height)
                    )
                    if not writer.isOpened():
                        raise RuntimeError("VideoWriter failed to open")
                    if scenario == "encode_async":
                        writer_queue = queue.Queue(maxsize=4)

                        def write_frames():
                            try:
                                while True:
                                    item = writer_queue.get()
                                    if item is None:
                                        break
                                    queued_frame, count = item
                                    for _ in range(count):
                                        writer.write(queued_frame)
                            except Exception as exc:
                                writer_error.append(exc)

                        writer_thread = threading.Thread(target=write_frames)
                        writer_thread.start()
                write_started = time.perf_counter()
                if scenario == "encode_async":
                    writer_queue.put((frame, due_frames))
                else:
                    for _ in range(due_frames):
                        writer.write(frame)
                write_time = time.perf_counter() - write_started
            timings["write"].append(write_time)
            output_frames += due_frames

            processed = time.perf_counter()
            timings["process"].append(processed - cycle_started)
            wait_time = 0.0
            if scheduler is None:
                wait_time = max(0.0, frame_period - (processed - cycle_started))
                if wait_time:
                    time.sleep(wait_time)
            finished = time.perf_counter()
            if scheduler is None:
                timings["wait"].append(finished - processed)
            timings["cycle"].append(finished - cycle_started)
    finally:
        if writer_queue is not None:
            writer_queue.put(None)
        if writer_thread is not None:
            writer_thread.join()
        if writer is not None:
            writer.release()
        if session is not None:
            session.close()
    if writer_error:
        raise writer_error[0]

    elapsed = time.perf_counter() - started
    expected_frames = round(elapsed * fps)
    return {
        "backend": backend,
        "scenario": scenario,
        "schedule": schedule_mode,
        "size": f"{width}x{height}",
        "target_fps": fps,
        "frames": frame_count,
        "output_frames": output_frames,
        "expected_frames": expected_frames,
        "missed_frames": max(0, expected_frames - frame_count),
        "wall_seconds": elapsed,
        "actual_fps": frame_count / elapsed,
        "output_path": str(output_path) if output_path else None,
        "timings": {name: summarize(values) for name, values in timings.items()},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=1.5)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark_output"))
    parser.add_argument(
        "--backends", nargs="+", choices=("imagegrab", "mss"), default=("imagegrab", "mss")
    )
    parser.add_argument(
        "--scheduler", choices=("current", "absolute"), default="current"
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=("clean", "overlay", "encode", "encode_async"),
        default=("clean", "overlay", "encode"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for backend in args.backends:
        for width, height in ((1280, 720), (1920, 1080)):
            for fps in (15, 24, 30):
                for scenario in args.scenarios:
                    print(
                        f"RUN backend={backend} scenario={scenario} "
                        f"size={width}x{height} fps={fps}",
                        flush=True,
                    )
                    result = run_case(
                        backend,
                        scenario,
                        width,
                        height,
                        fps,
                        args.duration,
                        args.output_dir,
                        args.scheduler,
                    )
                    results.append(result)
                    print(
                        f"RESULT actual_fps={result['actual_fps']:.2f} "
                        f"output_frames={result['output_frames']} "
                        f"capture_avg_ms={result['timings']['capture']['avg_ms']:.2f} "
                        f"convert_avg_ms={result['timings']['convert']['avg_ms']:.2f} "
                        f"cursor_avg_ms={result['timings']['cursor_state']['avg_ms']:.2f} "
                        f"draw_avg_ms={result['timings']['draw']['avg_ms']:.2f} "
                        f"write_avg_ms={result['timings']['write']['avg_ms']:.2f} "
                        f"missed={result['missed_frames']}",
                        flush=True,
                    )

    json_path = args.output_dir / "results.json"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"JSON {json_path}", flush=True)


if __name__ == "__main__":
    main()
