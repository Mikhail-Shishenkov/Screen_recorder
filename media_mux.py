import re
import shutil
import subprocess
import sys
from pathlib import Path

from audio_capture import AUDIO_MICROPHONE, AUDIO_SYSTEM

SYSTEM_MIX_GAIN_DB = -3.0
MICROPHONE_MIX_GAIN_DB = 0.0
MIX_LIMIT = 0.95


class MediaMuxError(RuntimeError):
    pass


def build_mixed_audio_filter(system_index, microphone_index):
    return (
        f"[{system_index}:a]"
        "aresample=48000,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={SYSTEM_MIX_GAIN_DB}dB[system];"
        f"[{microphone_index}:a]"
        "aresample=48000,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={MICROPHONE_MIX_GAIN_DB}dB[microphone];"
        "[system][microphone]"
        "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        f"alimiter=limit={MIX_LIMIT}:attack=5:release=50:"
        "level=false:latency=true,"
        "apad[aout]"
    )


def find_ffmpeg():
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        bundled = Path(bundle_dir) / "ffmpeg.exe"
        if bundled.is_file():
            return str(bundled)
        raise MediaMuxError(f"Bundled FFmpeg was not found: {bundled}")

    adjacent = Path(sys.executable).resolve().parent / "ffmpeg.exe"
    if adjacent.is_file():
        return str(adjacent)

    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    raise MediaMuxError("FFmpeg was not found. Install it for development builds.")


def build_mux_command(
    ffmpeg,
    raw_video,
    final_video,
    audio_tracks,
    output_duration=None,
):
    command = [str(ffmpeg), "-y", "-i", str(raw_video)]
    for track in audio_tracks:
        command.extend(["-i", str(track.path)])

    command.extend(["-map", "0:v:0"])
    if not audio_tracks:
        command.extend(["-an", "-c:v", "libx264", "-pix_fmt", "yuv420p"])
        if output_duration is not None:
            command.extend(["-t", f"{output_duration:.6f}"])
        command.extend(["-movflags", "+faststart", str(final_video)])
        return command

    if len(audio_tracks) == 1:
        filter_graph = (
            "[1:a]"
            "aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            "apad[aout]"
        )
    else:
        source_indexes = {
            track.source: index
            for index, track in enumerate(audio_tracks, start=1)
        }
        system_index = source_indexes[AUDIO_SYSTEM]
        microphone_index = source_indexes[AUDIO_MICROPHONE]
        filter_graph = build_mixed_audio_filter(
            system_index,
            microphone_index,
        )

    command.extend(
        [
            "-filter_complex",
            filter_graph,
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-shortest",
        ]
    )
    if output_duration is not None:
        command.extend(["-t", f"{output_duration:.6f}"])
    command.extend(["-movflags", "+faststart", str(final_video)])
    return command


def measure_audio_levels(ffmpeg, media_path):
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-i",
        str(media_path),
        "-map",
        "0:a:0",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "NUL" if sys.platform == "win32" else "/dev/null",
    ]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr = result.stderr.decode(errors="replace")
    mean_match = re.search(r"mean_volume:\s*(-?[\d.]+) dB", stderr)
    peak_match = re.search(r"max_volume:\s*(-?[\d.]+) dB", stderr)
    if result.returncode != 0 or not mean_match or not peak_match:
        return None
    return float(mean_match.group(1)), float(peak_match.group(1))


def write_mixed_diagnostic_wav(
    ffmpeg,
    audio_tracks,
    output_path,
    output_duration,
):
    source_indexes = {
        track.source: index
        for index, track in enumerate(audio_tracks)
    }
    command = [str(ffmpeg), "-y"]
    for track in audio_tracks:
        command.extend(["-i", str(track.path)])
    command.extend(
        [
            "-filter_complex",
            build_mixed_audio_filter(
                source_indexes[AUDIO_SYSTEM],
                source_indexes[AUDIO_MICROPHONE],
            ),
            "-map",
            "[aout]",
            "-t",
            f"{output_duration:.6f}",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(output_path),
        ]
    )
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def mux_recording(
    raw_video,
    final_video,
    audio_tracks,
    output_duration=None,
    logger=None,
    diagnostics=False,
):
    logger = logger or (lambda message: None)
    ffmpeg = find_ffmpeg()
    command = build_mux_command(
        ffmpeg,
        Path(raw_video),
        Path(final_video),
        audio_tracks,
        output_duration=output_duration,
    )
    logger(f"FFmpeg started: {' '.join(command)}")
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    logger(f"FFmpeg exit code: {result.returncode}")
    if result.returncode != 0 or not Path(final_video).exists():
        stderr = result.stderr.decode(errors="replace")
        raise MediaMuxError(
            f"FFmpeg mux failed with code {result.returncode}: {stderr}"
        )
    if diagnostics:
        logger("Muxed audio format: codec=AAC rate=48000 channels=2")
        if len(audio_tracks) == 2:
            diagnostic_duration = output_duration
            if diagnostic_duration is None:
                diagnostic_duration = max(
                    track.duration for track in audio_tracks
                )
            diagnostic_path = Path(final_video).with_name(
                f"{Path(final_video).stem}_mixed_pre_aac_debug.wav"
            )
            diagnostic_result = write_mixed_diagnostic_wav(
                ffmpeg,
                audio_tracks,
                diagnostic_path,
                diagnostic_duration,
            )
            if diagnostic_result.returncode == 0:
                logger(f"Mixed diagnostic WAV: {diagnostic_path}")
            else:
                logger(
                    "Mixed diagnostic WAV failed: "
                    + diagnostic_result.stderr.decode(errors="replace")
                )
        levels = measure_audio_levels(ffmpeg, final_video)
        if levels is not None:
            logger(
                f"Muxed audio levels: rms={levels[0]:.2f}dBFS "
                f"peak={levels[1]:.2f}dBFS"
            )
    return result
