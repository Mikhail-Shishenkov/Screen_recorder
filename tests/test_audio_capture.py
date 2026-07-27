import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from audio_capture import (
    AUDIO_MICROPHONE,
    AUDIO_MIXED,
    AUDIO_OFF,
    AUDIO_SYSTEM,
    CHUNK_FRAMES,
    AudioCaptureError,
    AudioSession,
    AudioSourceSpec,
    MicrophoneProcessor,
    PyAudioBackend,
    PyAudioStreamHandle,
    StreamingLinearResampler,
    apply_microphone_gain,
)


class FakeStreamHandle:
    def __init__(self, channels, sample_value):
        self.channels = channels
        self.sample_value = sample_value
        self.closed = False
        self.read_calls = 0
        self.condition = threading.Condition()

    def read(self, frames):
        with self.condition:
            self.read_calls += 1
            self.condition.notify_all()
            self.condition.wait(timeout=0.005)
        sample = int(self.sample_value).to_bytes(2, "little", signed=True)
        return sample * frames * self.channels

    def wait_for_reads(self, count):
        with self.condition:
            return self.condition.wait_for(
                lambda: self.read_calls >= count,
                timeout=1.0,
            )

    def close(self):
        self.closed = True
        with self.condition:
            self.condition.notify_all()


class FakeAudioBackend:
    def __init__(self, fail_source=None):
        self.fail_source = fail_source
        self.discover_calls = []
        self.opened_sources = []
        self.opened_specs = []
        self.handles = []
        self.closed = False

    @staticmethod
    def _spec(source):
        return AudioSourceSpec(
            source=source,
            device_index=1 if source == AUDIO_MICROPHONE else 2,
            device_name=f"fake-{source}",
            sample_rate=48000,
            channels=1 if source == AUDIO_MICROPHONE else 2,
        )

    def discover_sources(self, mode):
        self.discover_calls.append(mode)
        if mode == AUDIO_MICROPHONE:
            return [self._spec(AUDIO_MICROPHONE)]
        if mode == AUDIO_SYSTEM:
            return [self._spec(AUDIO_SYSTEM)]
        if mode == AUDIO_MIXED:
            return [
                self._spec(AUDIO_SYSTEM),
                self._spec(AUDIO_MICROPHONE),
            ]
        return []

    def open_source(self, spec):
        if spec.source == self.fail_source:
            raise OSError(f"failed {spec.source}")
        self.opened_sources.append(spec.source)
        self.opened_specs.append(spec)
        handle = FakeStreamHandle(
            spec.channels,
            30 if spec.source == AUDIO_MICROPHONE else 101,
        )
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True


class FakePyAudio:
    def get_default_wasapi_loopback(self):
        return {
            "index": 25,
            "name": "Speakers [Loopback]",
            "defaultSampleRate": 48000,
            "maxInputChannels": 2,
            "isLoopbackDevice": True,
        }

    def get_default_wasapi_device(self, d_in=False):
        if not d_in:
            raise AssertionError("input endpoint was not requested")
        return {
            "index": 20,
            "name": "Microphone (Realtek)",
            "defaultSampleRate": 48000,
            "maxInputChannels": 2,
            "isLoopbackDevice": False,
        }


class FakeStereoMixPyAudio(FakePyAudio):
    def get_default_wasapi_device(self, d_in=False):
        info = super().get_default_wasapi_device(d_in=d_in)
        info["name"] = "Stereo Mix (Realtek)"
        return info


class AudioSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.prefix = Path(self.temp_dir.name) / "recording_audio"

    def tearDown(self):
        self.temp_dir.cleanup()

    def stop_session(self, session):
        session.request_stop()
        for handle in session.backend.handles:
            with handle.condition:
                handle.condition.notify_all()
        return session.stop()

    def test_sound_off_does_not_discover_or_create_files(self):
        backend = FakeAudioBackend()
        session = AudioSession(AUDIO_OFF, self.prefix, backend=backend)

        session.start()
        tracks = session.stop()

        self.assertEqual(backend.discover_calls, [])
        self.assertEqual(backend.opened_sources, [])
        self.assertEqual(tracks, [])
        self.assertEqual(list(Path(self.temp_dir.name).glob("*.wav")), [])

    def test_microphone_opens_microphone_stream(self):
        backend = FakeAudioBackend()
        session = AudioSession(AUDIO_MICROPHONE, self.prefix, backend=backend)
        session.start()

        tracks = self.stop_session(session)

        self.assertEqual(backend.opened_sources, [AUDIO_MICROPHONE])
        self.assertEqual([track.source for track in tracks], [AUDIO_MICROPHONE])
        self.assertTrue(backend.handles[0].closed)
        self.assertTrue(backend.closed)
        self.assertFalse(backend.opened_specs[0].is_loopback)
        self.assertEqual(backend.opened_specs[0].source, AUDIO_MICROPHONE)

    def test_mic_only_pcm_cannot_contain_system_source_blocks(self):
        backend = FakeAudioBackend()
        session = AudioSession(AUDIO_MICROPHONE, self.prefix, backend=backend)
        session.start()
        self.assertTrue(backend.handles[0].wait_for_reads(2))
        tracks = self.stop_session(session)

        self.assertEqual(backend.opened_sources, [AUDIO_MICROPHONE])
        with wave.open(str(tracks[0].path), "rb") as wav_file:
            samples = np.frombuffer(wav_file.readframes(16), dtype="<i2")
        self.assertTrue(np.all(samples != 101))
        self.assertTrue(np.all(samples > 30))

    def test_system_sound_opens_loopback_source(self):
        backend = FakeAudioBackend()
        session = AudioSession(AUDIO_SYSTEM, self.prefix, backend=backend)
        session.start()

        tracks = self.stop_session(session)

        self.assertEqual(backend.opened_sources, [AUDIO_SYSTEM])
        self.assertEqual([track.source for track in tracks], [AUDIO_SYSTEM])

    def test_mixed_mode_opens_both_sources(self):
        backend = FakeAudioBackend()
        session = AudioSession(AUDIO_MIXED, self.prefix, backend=backend)
        session.start()

        tracks = self.stop_session(session)

        self.assertCountEqual(
            backend.opened_sources,
            [AUDIO_SYSTEM, AUDIO_MICROPHONE],
        )
        self.assertEqual(
            {track.source for track in tracks},
            {AUDIO_SYSTEM, AUDIO_MICROPHONE},
        )
        self.assertEqual(
            len({worker.spec.device_index for worker in session.workers}),
            2,
        )

    def test_paused_blocks_are_excluded_from_duration(self):
        backend = FakeAudioBackend()
        session = AudioSession(AUDIO_MICROPHONE, self.prefix, backend=backend)
        session.set_paused(True)
        session.start()
        handle = backend.handles[0]
        self.assertTrue(handle.wait_for_reads(3))
        paused_reads = handle.read_calls

        session.set_paused(False)
        self.assertTrue(handle.wait_for_reads(paused_reads + 2))
        tracks = self.stop_session(session)

        track = tracks[0]
        self.assertGreater(track.blocks_received, 0)
        self.assertLess(track.duration, track.blocks_received * CHUNK_FRAMES / 48000)

    def test_second_source_error_releases_first_and_allows_new_session(self):
        failing_backend = FakeAudioBackend(fail_source=AUDIO_MICROPHONE)
        failing_session = AudioSession(
            AUDIO_MIXED,
            self.prefix,
            backend=failing_backend,
            ready_timeout=1.0,
        )

        with self.assertRaises(AudioCaptureError):
            failing_session.start()

        self.assertTrue(all(handle.closed for handle in failing_backend.handles))

        working_backend = FakeAudioBackend()
        working_session = AudioSession(
            AUDIO_MICROPHONE,
            self.prefix,
            backend=working_backend,
        )
        working_session.start()
        tracks = self.stop_session(working_session)
        self.assertEqual(len(tracks), 1)

    def test_three_cycles_create_new_workers(self):
        workers = []
        processors = []
        for cycle in range(3):
            backend = FakeAudioBackend()
            session = AudioSession(
                AUDIO_MICROPHONE,
                self.prefix.with_name(f"audio_{cycle}"),
                backend=backend,
            )
            session.start()
            workers.append(session.workers[0])
            processors.append(session.workers[0].microphone_processor)
            self.stop_session(session)
            session.cleanup()

        self.assertEqual(len({id(worker) for worker in workers}), 3)
        self.assertEqual(len({id(processor) for processor in processors}), 3)
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def test_microphone_pcm_keeps_expected_amplitude_after_conversion(self):
        samples = np.array([-1000, -500, 0, 500, 1000], dtype="<i2")

        processed = np.frombuffer(
            apply_microphone_gain(samples.tobytes(), gain_db=6.0),
            dtype="<i2",
        )

        expected_gain = 10.0 ** (6.0 / 20.0)
        np.testing.assert_allclose(
            processed,
            samples * expected_gain,
            atol=1,
        )

    def test_loud_microphone_gain_is_peak_limited(self):
        samples = np.array(
            [-32768, -30000, 30000, 32767],
            dtype="<i2",
        )

        processed = np.frombuffer(
            apply_microphone_gain(samples.tobytes()),
            dtype="<i2",
        )

        self.assertLessEqual(
            int(np.max(np.abs(processed.astype(np.int32)))),
            round(32767 * 0.95),
        )

    def test_gain_is_applied_only_to_microphone_track(self):
        backend = FakeAudioBackend()
        session = AudioSession(AUDIO_MIXED, self.prefix, backend=backend)
        session.start()
        for handle in backend.handles:
            self.assertTrue(handle.wait_for_reads(2))
        tracks = self.stop_session(session)

        by_source = {track.source: track for track in tracks}
        self.assertEqual(by_source[AUDIO_SYSTEM].gain_db, 0.0)
        self.assertGreater(by_source[AUDIO_MICROPHONE].gain_db, 0.0)
        with wave.open(str(by_source[AUDIO_SYSTEM].path), "rb") as wav_file:
            system_sample = np.frombuffer(
                wav_file.readframes(1),
                dtype="<i2",
            )[0]
        with wave.open(str(by_source[AUDIO_MICROPHONE].path), "rb") as wav_file:
            microphone_sample = np.frombuffer(
                wav_file.readframes(1),
                dtype="<i2",
            )[0]
        self.assertEqual(system_sample, 101)
        self.assertGreater(microphone_sample, system_sample)

    def test_mixed_stop_releases_both_devices_and_next_session_starts(self):
        first_backend = FakeAudioBackend()
        first = AudioSession(AUDIO_MIXED, self.prefix, backend=first_backend)
        first.start()
        self.stop_session(first)

        second_backend = FakeAudioBackend()
        second = AudioSession(
            AUDIO_MIXED,
            self.prefix.with_name("second"),
            backend=second_backend,
        )
        second.start()
        self.stop_session(second)

        self.assertTrue(first_backend.closed)
        self.assertTrue(second_backend.closed)
        self.assertTrue(all(handle.closed for handle in first_backend.handles))
        self.assertTrue(all(handle.closed for handle in second_backend.handles))

    def test_system_then_microphone_uses_fresh_handles(self):
        system_backend = FakeAudioBackend()
        system_session = AudioSession(
            AUDIO_SYSTEM,
            self.prefix.with_name("system"),
            backend=system_backend,
        )
        system_session.start()
        self.stop_session(system_session)

        microphone_backend = FakeAudioBackend()
        microphone_session = AudioSession(
            AUDIO_MICROPHONE,
            self.prefix.with_name("microphone"),
            backend=microphone_backend,
        )
        microphone_session.start()
        self.stop_session(microphone_session)

        self.assertTrue(system_backend.handles[0].closed)
        self.assertTrue(microphone_backend.handles[0].closed)
        self.assertIsNot(system_backend.handles[0], microphone_backend.handles[0])
        self.assertEqual(microphone_backend.opened_sources, [AUDIO_MICROPHONE])

    def test_mixed_then_microphone_releases_old_sources(self):
        mixed_backend = FakeAudioBackend()
        mixed_session = AudioSession(AUDIO_MIXED, self.prefix, backend=mixed_backend)
        mixed_session.start()
        self.stop_session(mixed_session)

        microphone_backend = FakeAudioBackend()
        microphone_session = AudioSession(
            AUDIO_MICROPHONE,
            self.prefix.with_name("after_mixed"),
            backend=microphone_backend,
        )
        microphone_session.start()
        self.stop_session(microphone_session)

        self.assertTrue(all(handle.closed for handle in mixed_backend.handles))
        self.assertEqual(microphone_backend.opened_sources, [AUDIO_MICROPHONE])

    def test_three_mode_cycles_are_isolated(self):
        sessions = []
        for index, mode in enumerate((AUDIO_SYSTEM, AUDIO_MIXED, AUDIO_MICROPHONE)):
            backend = FakeAudioBackend()
            session = AudioSession(
                mode,
                self.prefix.with_name(f"cycle_{index}"),
                backend=backend,
            )
            session.start()
            self.stop_session(session)
            sessions.append((backend, session))

        self.assertTrue(
            all(
                all(handle.closed for handle in backend.handles)
                for backend, _ in sessions
            )
        )
        self.assertEqual(
            sessions[-1][0].opened_sources,
            [AUDIO_MICROPHONE],
        )


class PyAudioDiscoveryTests(unittest.TestCase):
    def test_microphone_uses_input_and_system_uses_loopback(self):
        backend = PyAudioBackend()
        backend.audio = FakePyAudio()

        microphone = backend.discover_sources(AUDIO_MICROPHONE)[0]
        system = backend.discover_sources(AUDIO_SYSTEM)[0]

        self.assertFalse(microphone.is_loopback)
        self.assertTrue(system.is_loopback)
        self.assertNotEqual(microphone.device_index, system.device_index)

    def test_mixed_uses_two_distinct_endpoints(self):
        backend = PyAudioBackend()
        backend.audio = FakePyAudio()

        sources = backend.discover_sources(AUDIO_MIXED)

        self.assertEqual(
            [source.source for source in sources],
            [AUDIO_SYSTEM, AUDIO_MICROPHONE],
        )
        self.assertEqual(len({source.device_index for source in sources}), 2)

    def test_microphone_rejects_stereo_mix_endpoint(self):
        backend = PyAudioBackend()
        backend.audio = FakeStereoMixPyAudio()

        with self.assertRaises(AudioCaptureError):
            backend.discover_sources(AUDIO_MICROPHONE)


class StreamingAudioProcessingTests(unittest.TestCase):
    @staticmethod
    def sine(sample_rate, duration=1.0, frequency=997.0):
        frame_count = round(sample_rate * duration)
        time_axis = np.arange(frame_count) / sample_rate
        return np.sin(2 * np.pi * frequency * time_axis)

    @staticmethod
    def resample_in_chunks(samples, chunk_sizes):
        resampler = StreamingLinearResampler(44100, 48000)
        output = []
        offset = 0
        size_index = 0
        while offset < len(samples):
            size = chunk_sizes[size_index % len(chunk_sizes)]
            output.append(resampler.process(samples[offset : offset + size]))
            offset += size
            size_index += 1
        output.append(resampler.process([], final=True))
        return np.concatenate(output).ravel()

    def test_streaming_44100_to_48000_sine_has_no_block_discontinuity(self):
        source = self.sine(44100)
        reference = StreamingLinearResampler(44100, 48000).process(
            source,
            final=True,
        ).ravel()

        fragmented = self.resample_in_chunks(source, [127, 480, 1024, 333])

        self.assertEqual(len(fragmented), 48000)
        np.testing.assert_allclose(fragmented, reference, atol=1e-10)

    def test_resampler_output_is_independent_of_input_block_sizes(self):
        source = self.sine(44100)

        small_blocks = self.resample_in_chunks(source, [73, 211])
        varied_blocks = self.resample_in_chunks(source, [1024, 17, 509])

        np.testing.assert_allclose(small_blocks, varied_blocks, atol=1e-10)

    def test_microphone_processing_is_continuous_across_blocks(self):
        source = np.rint(
            self.sine(48000, frequency=731.0) * 2500
        ).astype("<i2")
        reference = MicrophoneProcessor().process(source.tobytes())
        processor = MicrophoneProcessor()
        fragmented = b"".join(
            processor.process(source[offset : offset + 257].tobytes())
            for offset in range(0, len(source), 257)
        )

        self.assertEqual(fragmented, reference)

    def test_new_recording_cycles_start_with_clean_resampler_state(self):
        source = self.sine(44100, duration=0.1)
        outputs = [
            StreamingLinearResampler(44100, 48000).process(
                source,
                final=True,
            )
            for _ in range(3)
        ]

        np.testing.assert_array_equal(outputs[0], outputs[1])
        np.testing.assert_array_equal(outputs[1], outputs[2])


class CallbackIsolationTests(unittest.TestCase):
    def test_closed_microphone_queue_cannot_feed_next_recording(self):
        previous = PyAudioStreamHandle(AUDIO_MICROPHONE)
        previous.callback(b"\x65\x00" * CHUNK_FRAMES, CHUNK_FRAMES, {}, 0)
        previous.close()
        current = PyAudioStreamHandle(AUDIO_MICROPHONE)

        self.assertTrue(previous.closed)
        self.assertTrue(previous.blocks.empty())
        self.assertIsNot(previous.blocks, current.blocks)
        self.assertEqual(
            previous.callback(b"\x65\x00", 1, {}, 0)[1],
            1,
        )


if __name__ == "__main__":
    unittest.main()
