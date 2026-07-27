import queue
import threading
import wave
from dataclasses import dataclass
from math import log10
from pathlib import Path

import numpy as np
import pyaudiowpatch as pyaudio
import soundcard


AUDIO_OFF = "off"
AUDIO_SYSTEM = "system"
AUDIO_MICROPHONE = "microphone"
AUDIO_MIXED = "system_microphone"

AUDIO_MODE_LABELS = (
    ("Выкл.", AUDIO_OFF),
    ("Система", AUDIO_SYSTEM),
    ("Микр.", AUDIO_MICROPHONE),
    ("Сист+мик", AUDIO_MIXED),
)

CHUNK_FRAMES = 1024
SAMPLE_FORMAT = pyaudio.paInt16
SAMPLE_WIDTH = 2
TARGET_SAMPLE_RATE = 48000
MICROPHONE_GAIN_DB = 15.0
MICROPHONE_LIMIT = 0.95


class AudioCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioSourceSpec:
    source: str
    device_index: int
    device_name: str
    sample_rate: int
    channels: int
    backend: str = "WASAPI"
    sample_format: str = "signed int16"
    sample_width: int = SAMPLE_WIDTH
    is_loopback: bool = False
    endpoint_type: str = "capture-input"
    backend_device_id: str = ""


@dataclass(frozen=True)
class AudioTrack:
    source: str
    path: Path
    sample_rate: int
    channels: int
    duration: float
    blocks_received: int
    blocks_lost: int
    device_index: int = -1
    device_name: str = ""
    backend: str = ""
    sample_format: str = ""
    gain_db: float = 0.0
    raw_rms_dbfs: float = float("-inf")
    raw_peak_dbfs: float = float("-inf")
    processed_rms_dbfs: float = float("-inf")
    processed_peak_dbfs: float = float("-inf")


def amplitude_to_dbfs(value):
    if value <= 0:
        return float("-inf")
    return 20.0 * log10(min(float(value), 1.0))


class PcmLevelMeter:
    def __init__(self):
        self.sample_count = 0
        self.sum_squares = 0.0
        self.peak = 0.0

    def add_pcm16(self, data):
        samples = np.frombuffer(data, dtype="<i2")
        if samples.size == 0:
            return
        normalized = samples.astype(np.float64) / 32768.0
        self.sample_count += normalized.size
        self.sum_squares += float(np.dot(normalized, normalized))
        self.peak = max(self.peak, float(np.max(np.abs(normalized))))

    @property
    def rms_dbfs(self):
        if self.sample_count == 0:
            return float("-inf")
        rms = (self.sum_squares / self.sample_count) ** 0.5
        return amplitude_to_dbfs(rms)

    @property
    def peak_dbfs(self):
        return amplitude_to_dbfs(self.peak)


def apply_microphone_gain(
    data,
    gain_db=MICROPHONE_GAIN_DB,
    limit=MICROPHONE_LIMIT,
):
    processor = MicrophoneProcessor(gain_db=gain_db, limit=limit)
    return processor.process(data)


class MicrophoneProcessor:
    """Preserve microphone processing state across PortAudio blocks."""

    def __init__(
        self,
        gain_db=MICROPHONE_GAIN_DB,
        limit=MICROPHONE_LIMIT,
    ):
        self.gain_db = gain_db
        self.limit = limit
        self.last_input_sample = None
        self.last_output_sample = None
        self.input_boundary_jump = 0.0
        self.output_boundary_jump = 0.0
        self.max_input_boundary_jump = 0.0
        self.max_output_boundary_jump = 0.0

    def process(self, data):
        samples = np.frombuffer(data, dtype="<i2")
        if samples.size == 0:
            return data

        normalized = samples.astype(np.float32) / np.float32(32768.0)
        if self.last_input_sample is not None:
            self.input_boundary_jump = abs(
                float(normalized[0]) - self.last_input_sample
            )
            self.max_input_boundary_jump = max(
                self.max_input_boundary_jump,
                self.input_boundary_jump,
            )

        gain = np.float32(10.0 ** (self.gain_db / 20.0))
        gained = normalized * gain
        magnitude = np.abs(gained)
        knee_start = self.limit * 0.9
        over_limit = magnitude > knee_start
        if np.any(over_limit):
            headroom = self.limit - knee_start
            magnitude[over_limit] = knee_start + headroom * np.tanh(
                (magnitude[over_limit] - knee_start) / headroom
            )
            gained = np.copysign(magnitude, gained)

        output = np.rint(np.clip(gained, -1.0, 1.0) * 32767.0)
        output_samples = output.astype("<i2")
        output_normalized = output_samples.astype(np.float64) / 32768.0
        if self.last_output_sample is not None:
            self.output_boundary_jump = abs(
                float(output_normalized[0]) - self.last_output_sample
            )
            self.max_output_boundary_jump = max(
                self.max_output_boundary_jump,
                self.output_boundary_jump,
            )
        self.last_input_sample = float(normalized[-1])
        self.last_output_sample = float(output_normalized[-1])
        return output_samples.tobytes()


class StreamingLinearResampler:
    """Stateful linear resampler used when a source cannot open at 48 kHz."""

    def __init__(self, input_rate, output_rate, channels=1):
        self.input_rate = int(input_rate)
        self.output_rate = int(output_rate)
        self.channels = int(channels)
        self.step = self.input_rate / self.output_rate
        self.position = 0.0
        self.history = np.empty((0, self.channels), dtype=np.float64)

    @property
    def remainder_samples(self):
        return len(self.history)

    def process(self, samples, final=False):
        values = np.asarray(samples, dtype=np.float64).reshape(
            -1,
            self.channels,
        )
        if self.history.size:
            values = np.concatenate((self.history, values), axis=0)
        if values.size == 0:
            return values

        limit = len(values) if final else len(values) - 1
        if limit <= 0 or self.position >= limit:
            self.history = values
            return np.empty((0, self.channels), dtype=np.float64)

        positions = np.arange(self.position, limit, self.step)
        left = np.floor(positions).astype(np.int64)
        fraction = (positions - left)[:, None]
        right = np.minimum(left + 1, len(values) - 1)
        output = values[left] * (1.0 - fraction) + values[right] * fraction

        consumed = int(np.floor(positions[-1])) if positions.size else 0
        self.history = values[consumed:]
        self.position = positions[-1] + self.step - consumed
        return output


class PyAudioStreamHandle:
    def __init__(self, source):
        self.source = source
        self.stream = None
        self.blocks = queue.Queue()
        self.status_flags = []
        self.closed = False

    def callback(self, in_data, frame_count, time_info, status_flags):
        if self.closed:
            return None, pyaudio.paComplete
        if status_flags:
            self.status_flags.append(int(status_flags))
        self.blocks.put((in_data, int(frame_count)))
        return None, pyaudio.paContinue

    def attach_stream(self, stream):
        self.stream = stream
        self.stream.start_stream()

    def read(self, frames):
        try:
            data, frame_count = self.blocks.get(timeout=0.25)
        except queue.Empty as exc:
            raise OSError("Timed out waiting for a PortAudio callback") from exc
        if frame_count != frames:
            raise OSError(
                f"PortAudio callback returned {frame_count} frames, "
                f"expected {frames}"
            )
        return data

    def close(self):
        self.closed = True
        self.blocks = queue.Queue()
        if self.stream is None:
            return
        try:
            if self.stream.is_active():
                self.stream.stop_stream()
        finally:
            self.stream.close()


class BlockingPyAudioStreamHandle:
    def __init__(self, stream):
        self.stream = stream

    def read(self, frames):
        return self.stream.read(frames, exception_on_overflow=False)

    def close(self):
        try:
            if self.stream.is_active():
                self.stream.stop_stream()
        finally:
            self.stream.close()


class PyAudioBackend:
    def __init__(self):
        self.audio = None
        self.lock = threading.Lock()

    def _get_audio(self):
        if self.audio is None:
            self.audio = pyaudio.PyAudio()
        return self.audio

    @staticmethod
    def _is_ordinary_microphone(info):
        name = str(info["name"]).casefold()
        rejected = (
            "loopback",
            "stereo mix",
            "стерео микшер",
            "what u hear",
            "wave out mix",
        )
        return not bool(info.get("isLoopbackDevice", False)) and not any(
            token in name for token in rejected
        )

    @staticmethod
    def _windows_endpoint_id(device_name, is_loopback):
        try:
            endpoints = soundcard.all_microphones(include_loopback=True)
        except Exception:
            return ""
        for endpoint in endpoints:
            if (
                endpoint.name == device_name
                and bool(endpoint.isloopback) == bool(is_loopback)
            ):
                return endpoint.id
        return ""

    def discover_sources(self, mode):
        if mode == AUDIO_OFF:
            return []

        with self.lock:
            audio = self._get_audio()
            sources = []
            try:
                if mode in (AUDIO_SYSTEM, AUDIO_MIXED):
                    info = audio.get_default_wasapi_loopback()
                    sources.append(
                        AudioSourceSpec(
                            source=AUDIO_SYSTEM,
                            device_index=int(info["index"]),
                            device_name=str(info["name"]),
                            sample_rate=int(info["defaultSampleRate"]),
                            channels=min(2, max(1, int(info["maxInputChannels"]))),
                            is_loopback=True,
                            endpoint_type="capture-loopback",
                            backend_device_id=(
                                self._windows_endpoint_id(
                                    str(info["name"]),
                                    True,
                                )
                                or f"wasapi-device-index:{int(info['index'])}"
                            ),
                        )
                    )
                if mode in (AUDIO_MICROPHONE, AUDIO_MIXED):
                    info = audio.get_default_wasapi_device(d_in=True)
                    if not self._is_ordinary_microphone(info):
                        raise AudioCaptureError(
                            "Default microphone endpoint is not an ordinary input"
                        )
                    sources.append(
                        AudioSourceSpec(
                            source=AUDIO_MICROPHONE,
                            device_index=int(info["index"]),
                            device_name=str(info["name"]),
                            sample_rate=int(info["defaultSampleRate"]),
                            channels=1,
                            backend="WASAPI",
                            endpoint_type="capture-input",
                            backend_device_id=(
                                self._windows_endpoint_id(
                                    str(info["name"]),
                                    False,
                                )
                                or f"wasapi-device-index:{int(info['index'])}"
                            ),
                        )
                    )
                if (
                    mode == AUDIO_MIXED
                    and sources[0].device_index == sources[1].device_index
                ):
                    raise AudioCaptureError(
                        "System and microphone sources use the same endpoint"
                    )
                return sources
            except Exception as exc:
                raise AudioCaptureError(
                    f"Audio device discovery failed: {exc}"
                ) from exc

    def open_source(self, spec):
        with self.lock:
            audio = self._get_audio()
            if spec.source == AUDIO_MICROPHONE:
                info = audio.get_device_info_by_index(spec.device_index)
                if (
                    spec.is_loopback
                    or spec.endpoint_type != "capture-input"
                    or not self._is_ordinary_microphone(info)
                ):
                    raise AudioCaptureError(
                        "Refusing to open a non-microphone endpoint for mic-only"
                    )
            if spec.source == AUDIO_SYSTEM:
                stream = audio.open(
                    format=SAMPLE_FORMAT,
                    channels=spec.channels,
                    rate=spec.sample_rate,
                    input=True,
                    input_device_index=spec.device_index,
                    frames_per_buffer=CHUNK_FRAMES,
                    start=True,
                )
                return BlockingPyAudioStreamHandle(stream)

            handle = PyAudioStreamHandle(spec.source)
            stream = audio.open(
                format=SAMPLE_FORMAT,
                channels=spec.channels,
                rate=spec.sample_rate,
                input=True,
                input_device_index=spec.device_index,
                frames_per_buffer=CHUNK_FRAMES,
                stream_callback=handle.callback,
                start=False,
            )
            handle.attach_stream(stream)
            return handle

    def close(self):
        with self.lock:
            if self.audio is not None:
                self.audio.terminate()
                self.audio = None


class AudioCaptureWorker(threading.Thread):
    def __init__(
        self,
        spec,
        output_path,
        backend,
        pause_event,
        logger,
        diagnostics=False,
        stream_handle=None,
    ):
        super().__init__(name=f"audio-{spec.source}")
        self.spec = spec
        self.output_path = Path(output_path)
        self.backend = backend
        self.pause_event = pause_event
        self.logger = logger
        self.diagnostics = diagnostics
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.error = None
        self.blocks_received = 0
        self.blocks_lost = 0
        self.consecutive_errors = 0
        self.written_frames = 0
        self.stream_handle = stream_handle
        self.gain_db = (
            MICROPHONE_GAIN_DB
            if self.spec.source == AUDIO_MICROPHONE
            else 0.0
        )
        self.raw_levels = PcmLevelMeter()
        self.processed_levels = PcmLevelMeter()
        self.microphone_processor = (
            MicrophoneProcessor(gain_db=self.gain_db)
            if self.spec.source == AUDIO_MICROPHONE
            else None
        )

    @property
    def duration(self):
        return self.written_frames / self.spec.sample_rate

    def request_stop(self):
        self.stop_event.set()

    def run(self):
        wav_file = None
        raw_debug_wav = None
        processed_debug_wav = None
        try:
            if self.stream_handle is None:
                self.stream_handle = self.backend.open_source(self.spec)
            wav_file = wave.open(str(self.output_path), "wb")
            wav_file.setnchannels(self.spec.channels)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(self.spec.sample_rate)
            self.logger(
                f"Audio source opened: source={self.spec.source} "
                f"device={self.spec.device_name!r} index={self.spec.device_index} "
                f"device_id={self.spec.backend_device_id or self.spec.device_index} "
                f"backend={self.spec.backend} type={self.spec.endpoint_type} "
                f"loopback={self.spec.is_loopback} "
                f"rate={self.spec.sample_rate} channels={self.spec.channels} "
                f"target_rate={TARGET_SAMPLE_RATE} "
                f"format={self.spec.sample_format} bits={self.spec.sample_width * 8} "
                f"chunk={CHUNK_FRAMES} gain={self.gain_db:+.1f}dB"
            )
            if self.diagnostics and self.spec.source == AUDIO_MICROPHONE:
                raw_debug_path = self.output_path.with_name(
                    f"{self.output_path.stem}_raw_debug.wav"
                )
                processed_debug_path = self.output_path.with_name(
                    f"{self.output_path.stem}_processed_debug.wav"
                )
                raw_debug_wav = wave.open(str(raw_debug_path), "wb")
                processed_debug_wav = wave.open(
                    str(processed_debug_path),
                    "wb",
                )
                for debug_wav in (raw_debug_wav, processed_debug_wav):
                    debug_wav.setnchannels(self.spec.channels)
                    debug_wav.setsampwidth(SAMPLE_WIDTH)
                    debug_wav.setframerate(self.spec.sample_rate)
                self.logger(
                    f"Microphone diagnostic WAVs: raw={raw_debug_path} "
                    f"processed={processed_debug_path}"
                )
            self.ready_event.set()

            while not self.stop_event.is_set():
                try:
                    data = self.stream_handle.read(CHUNK_FRAMES)
                    self.blocks_received += 1
                    self.consecutive_errors = 0
                except (IOError, OSError) as exc:
                    self.blocks_lost += 1
                    self.consecutive_errors += 1
                    self.logger(
                        f"Audio block lost: source={self.spec.source} error={exc}"
                    )
                    if self.consecutive_errors >= 10:
                        raise AudioCaptureError(
                            f"Audio source {self.spec.source} repeatedly failed"
                        ) from exc
                    continue

                if self.stop_event.is_set():
                    break
                if self.pause_event.is_set():
                    continue
                self.raw_levels.add_pcm16(data)
                if raw_debug_wav is not None:
                    raw_debug_wav.writeframesraw(data)
                processed_data = (
                    self.microphone_processor.process(data)
                    if self.spec.source == AUDIO_MICROPHONE
                    else data
                )
                self.processed_levels.add_pcm16(processed_data)
                if processed_debug_wav is not None:
                    processed_debug_wav.writeframesraw(processed_data)
                wav_file.writeframesraw(processed_data)
                bytes_per_frame = SAMPLE_WIDTH * self.spec.channels
                self.written_frames += len(processed_data) // bytes_per_frame
                if self.diagnostics:
                    self.logger(
                        f"Audio block: source={self.spec.source} "
                        f"input_frames={len(data) // bytes_per_frame} "
                        f"output_frames={len(processed_data) // bytes_per_frame} "
                        "remainder=0 "
                        f"boundary_in={getattr(self.microphone_processor, 'input_boundary_jump', 0.0):.6f} "
                        f"boundary_out={getattr(self.microphone_processor, 'output_boundary_jump', 0.0):.6f}"
                    )
        except Exception as exc:
            self.error = AudioCaptureError(
                f"Audio source {self.spec.source} failed: {exc}"
            )
            self.ready_event.set()
        finally:
            if wav_file is not None:
                wav_file.close()
            if raw_debug_wav is not None:
                raw_debug_wav.close()
            if processed_debug_wav is not None:
                processed_debug_wav.close()
            if self.stream_handle is not None:
                try:
                    self.stream_handle.close()
                except Exception as exc:
                    if self.error is None:
                        self.error = AudioCaptureError(
                            f"Audio source {self.spec.source} close failed: {exc}"
                        )
            if (
                self.stream_handle is not None
                and getattr(self.stream_handle, "status_flags", None)
            ):
                self.logger(
                    f"PortAudio callback status: source={self.spec.source} "
                    f"flags={self.stream_handle.status_flags}"
                )
            self.logger(
                f"Audio source stopped: source={self.spec.source} "
                f"blocks={self.blocks_received} lost={self.blocks_lost} "
                f"duration={self.duration:.3f}s gain={self.gain_db:+.1f}dB "
                f"raw_rms={self.raw_levels.rms_dbfs:.2f}dBFS "
                f"raw_peak={self.raw_levels.peak_dbfs:.2f}dBFS "
                f"processed_rms={self.processed_levels.rms_dbfs:.2f}dBFS "
                f"processed_peak={self.processed_levels.peak_dbfs:.2f}dBFS "
                f"boundary_in={getattr(self.microphone_processor, 'max_input_boundary_jump', 0.0):.6f} "
                f"boundary_out={getattr(self.microphone_processor, 'max_output_boundary_jump', 0.0):.6f}"
            )


class AudioSession:
    def __init__(
        self,
        mode,
        output_prefix,
        backend=None,
        logger=None,
        ready_timeout=5.0,
        diagnostics=False,
    ):
        self.mode = mode
        self.output_prefix = Path(output_prefix)
        self.backend = backend or PyAudioBackend()
        self.logger = logger or (lambda message: None)
        self.ready_timeout = ready_timeout
        self.diagnostics = diagnostics
        self.pause_event = threading.Event()
        self.workers = []
        self.started = False
        self.stopped = False

    def start(self):
        if self.started:
            raise AudioCaptureError("Audio session has already been started")
        self.started = True
        self.logger(f"Audio mode selected: {self.mode}")
        if self.mode == AUDIO_OFF:
            return

        specs = self.backend.discover_sources(self.mode)
        expected_sources = 2 if self.mode == AUDIO_MIXED else 1
        if len(specs) != expected_sources:
            raise AudioCaptureError(
                f"Expected {expected_sources} audio source(s), found {len(specs)}"
            )
        microphone_specs = [
            spec for spec in specs if spec.source == AUDIO_MICROPHONE
        ]
        if self.mode == AUDIO_MICROPHONE and (
            len(microphone_specs) != 1
            or microphone_specs[0].is_loopback
            or microphone_specs[0].endpoint_type != "capture-input"
        ):
            raise AudioCaptureError(
                "Mic-only mode requires exactly one ordinary input endpoint"
            )

        opened_handles = []
        microphone_handle = None
        try:
            for spec in specs:
                if spec.source != AUDIO_MICROPHONE:
                    continue
                try:
                    microphone_handle = self.backend.open_source(spec)
                    opened_handles.append(microphone_handle)
                except Exception as exc:
                    raise AudioCaptureError(
                        f"Audio source {spec.source} failed: {exc}"
                    ) from exc

            for spec in specs:
                stream_handle = (
                    microphone_handle
                    if spec.source == AUDIO_MICROPHONE
                    else None
                )
                path = self.output_prefix.with_name(
                    f"{self.output_prefix.name}_{spec.source}.wav"
                )
                worker = AudioCaptureWorker(
                    spec,
                    path,
                    self.backend,
                    self.pause_event,
                    self.logger,
                    diagnostics=self.diagnostics,
                    stream_handle=stream_handle,
                )
                self.workers.append(worker)
                worker.start()

            for worker in self.workers:
                if not worker.ready_event.wait(self.ready_timeout):
                    raise AudioCaptureError(
                        f"Audio source {worker.spec.source} start timed out"
                    )
                if worker.error is not None:
                    raise worker.error
        except Exception:
            self.request_stop()
            try:
                self._join_workers()
            finally:
                worker_handles = {
                    worker.stream_handle for worker in self.workers
                }
                for handle in opened_handles:
                    if handle in worker_handles:
                        continue
                    try:
                        handle.close()
                    except Exception:
                        pass
                self._close_backend()
                self.cleanup()
            raise

    def set_paused(self, paused):
        if paused:
            self.pause_event.set()
        else:
            self.pause_event.clear()
        self.logger(f"Audio pause state: paused={paused}")

    def request_stop(self):
        for worker in self.workers:
            worker.request_stop()

    def _join_workers(self):
        errors = []
        for worker in self.workers:
            worker.join(self.ready_timeout)
            if worker.is_alive():
                errors.append(
                    AudioCaptureError(
                        f"Audio source {worker.spec.source} did not stop"
                    )
                )
            elif worker.error is not None:
                errors.append(worker.error)
        if errors:
            raise errors[0]

    def stop(self):
        if self.stopped:
            return self.tracks()
        self.stopped = True
        self.request_stop()
        try:
            self._join_workers()
        finally:
            self._close_backend()
        tracks = self.tracks()
        self.logger(
            "Audio session stopped: "
            + ", ".join(
                f"{track.source}={track.duration:.3f}s" for track in tracks
            )
        )
        return tracks

    def _close_backend(self):
        close = getattr(self.backend, "close", None)
        if close is not None:
            close()

    def tracks(self):
        return [
            AudioTrack(
                source=worker.spec.source,
                path=worker.output_path,
                sample_rate=worker.spec.sample_rate,
                channels=worker.spec.channels,
                duration=worker.duration,
                blocks_received=worker.blocks_received,
                blocks_lost=worker.blocks_lost,
                device_index=worker.spec.device_index,
                device_name=worker.spec.device_name,
                backend=worker.spec.backend,
                sample_format=worker.spec.sample_format,
                gain_db=worker.gain_db,
                raw_rms_dbfs=worker.raw_levels.rms_dbfs,
                raw_peak_dbfs=worker.raw_levels.peak_dbfs,
                processed_rms_dbfs=worker.processed_levels.rms_dbfs,
                processed_peak_dbfs=worker.processed_levels.peak_dbfs,
            )
            for worker in self.workers
            if worker.output_path.exists()
        ]

    def cleanup(self):
        for worker in self.workers:
            try:
                worker.output_path.unlink()
                self.logger(f"Audio temporary file removed: {worker.output_path}")
            except FileNotFoundError:
                continue
