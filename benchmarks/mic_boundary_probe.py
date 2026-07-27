import argparse
import json
import wave
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio

from audio_capture import apply_microphone_gain


def write_wav(path, samples, sample_rate):
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.astype("<i2").tobytes())


def jump_metrics(samples, boundaries):
    normalized = samples.astype(np.float64) / 32768.0
    jumps = np.abs(np.diff(normalized))
    valid = np.asarray(
        [index for index in boundaries if 0 < index < normalized.size],
        dtype=np.int64,
    )
    boundary_indexes = valid - 1
    boundary_jumps = jumps[boundary_indexes]
    interior_mask = np.ones(jumps.size, dtype=bool)
    interior_mask[boundary_indexes] = False
    interior_jumps = jumps[interior_mask]
    return {
        "boundary_count": int(boundary_jumps.size),
        "boundary_max": float(np.max(boundary_jumps, initial=0.0)),
        "boundary_p99": float(np.percentile(boundary_jumps, 99))
        if boundary_jumps.size
        else 0.0,
        "interior_p99": float(np.percentile(interior_jumps, 99))
        if interior_jumps.size
        else 0.0,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--chunk", type=int, default=1024)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark_output/mic_boundary_probe"),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    audio = pyaudio.PyAudio()
    stream = None
    try:
        device = audio.get_default_wasapi_device(d_in=True)
        sample_rate = int(device["defaultSampleRate"])
        stream = audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            input_device_index=int(device["index"]),
            frames_per_buffer=args.chunk,
        )
        chunks = []
        boundaries = []
        total_samples = 0
        block_count = round(args.duration * sample_rate / args.chunk)
        for _ in range(block_count):
            data = stream.read(args.chunk, exception_on_overflow=False)
            chunk = np.frombuffer(data, dtype="<i2").copy()
            chunks.append(chunk)
            total_samples += chunk.size
            boundaries.append(total_samples)
    finally:
        if stream is not None:
            stream.stop_stream()
            stream.close()
        audio.terminate()

    raw = np.concatenate(chunks)
    processed = np.frombuffer(
        apply_microphone_gain(raw.astype("<i2").tobytes()),
        dtype="<i2",
    ).copy()
    raw_path = args.output_dir / f"microphone_raw_chunk_{args.chunk}.wav"
    processed_path = (
        args.output_dir / f"microphone_processed_chunk_{args.chunk}.wav"
    )
    write_wav(raw_path, raw, sample_rate)
    write_wav(processed_path, processed, sample_rate)

    packet_boundaries = range(480, raw.size, 480)
    result = {
        "device_index": int(device["index"]),
        "device_name": str(device["name"]),
        "sample_rate": sample_rate,
        "chunk_frames": args.chunk,
        "input_samples_per_block": sorted({chunk.size for chunk in chunks}),
        "output_samples_per_block": sorted({chunk.size for chunk in chunks}),
        "remainder_samples": 0,
        "raw_read_boundaries": jump_metrics(raw, boundaries),
        "processed_read_boundaries": jump_metrics(processed, boundaries),
        "raw_480_boundaries": jump_metrics(raw, packet_boundaries),
        "processed_480_boundaries": jump_metrics(processed, packet_boundaries),
        "raw_wav": str(raw_path),
        "processed_wav": str(processed_path),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
