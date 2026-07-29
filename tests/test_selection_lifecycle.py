import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, QEvent, QSettings
from PyQt5.QtWidgets import QApplication

from audio_capture import AudioCaptureError
from ffmpeg_video_writer import VIDEO_PROFILES
from localization import translate
from media_mux import MediaMuxError
from screen_recorder import RECORDING_MODES, ScreenRecorder, bundled_resource_path


class FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in tuple(self._slots):
            slot(*args)


class FakeRecorderThread:
    def __init__(self, bbox, output_path, profile_key):
        self.bbox = bbox
        self.output_path = output_path
        self.profile_key = profile_key
        self.is_recording = True
        self.is_paused = False
        self.completed = FakeSignal()
        self.error = FakeSignal()
        self._running = False

    def start(self):
        self._running = True

    def isRunning(self):
        return self._running

    def complete(self):
        self.is_recording = False
        self._running = False
        self.completed.emit()


class FakeAudioSession:
    def __init__(self, fail=False):
        self.fail = fail
        self.started = False
        self.stopped = False
        self.cleaned = False
        self.paused_states = []

    def start(self):
        if self.fail:
            raise AudioCaptureError("second source failed")
        self.started = True

    def set_paused(self, paused):
        self.paused_states.append(paused)

    def request_stop(self):
        pass

    def stop(self):
        self.stopped = True
        return []

    def cleanup(self):
        self.cleaned = True


class SelectionLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.hotkeys_patcher = patch.object(ScreenRecorder, "init_hotkeys", lambda self: None)
        self.mkdir_patcher = patch("screen_recorder.Path.mkdir")
        self.worker_patcher = patch("screen_recorder.RecorderThread", FakeRecorderThread)
        self.hotkeys_patcher.start()
        self.mkdir_patcher.start()
        self.worker_patcher.start()
        self.settings_dir = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            str(Path(self.settings_dir.name) / "settings.ini"),
            QSettings.IniFormat,
        )
        self.recorder = ScreenRecorder(settings=self.settings)
        self.recorder.show()
        self.app.processEvents()

    def tearDown(self):
        if self.recorder.selector_overlay:
            self.recorder.selector_overlay.close()
        if self.recorder.border_window:
            self.recorder.border_window.close()
        self.recorder.close()
        self.app.processEvents()
        self.worker_patcher.stop()
        self.mkdir_patcher.stop()
        self.hotkeys_patcher.stop()
        self.settings_dir.cleanup()

    def finish_selection(self, rect):
        self.recorder.select_btn.click()
        self.app.processEvents()
        selector = self.recorder.selector_overlay
        self.assertIsNotNone(selector)
        self.assertTrue(selector.isVisible())
        selector.selection_made.emit(*rect)
        selector.close()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertIsNone(self.recorder.selector_overlay)

    def test_two_selections_restore_main_window_and_show_new_border(self):
        first_rect = (10, 20, 100, 100)
        second_rect = (30, 40, 120, 140)

        self.finish_selection(first_rect)
        first_border = self.recorder.border_window
        self.assertIsNotNone(first_border)
        self.assertTrue(first_border.isVisible())

        self.recorder.record_btn.click()
        worker = self.recorder.recorder_thread
        self.assertIsNotNone(worker)
        self.recorder.stop_btn.click()
        worker.complete()
        self.app.processEvents()

        self.finish_selection(second_rect)
        second_border = self.recorder.border_window

        self.assertIsNotNone(second_border)
        self.assertIsNot(second_border, first_border)
        self.assertTrue(second_border.isVisible())
        self.assertEqual(self.recorder.recording_rect, second_rect)
        self.assertTrue(self.recorder.isVisible())
        self.assertFalse(self.recorder.isMinimized())
        self.assertFalse(self.recorder.recording)
        self.assertTrue(self.recorder.record_btn.isEnabled())
        self.assertFalse(self.recorder.pause_btn.isEnabled())

    def test_cancel_restores_main_window(self):
        self.recorder.showMinimized()
        self.recorder.select_btn.click()
        self.app.processEvents()
        selector = self.recorder.selector_overlay

        selector.selection_canceled.emit()
        selector.close()
        self.app.processEvents()

        self.assertTrue(self.recorder.isVisible())
        self.assertFalse(self.recorder.isMinimized())
        self.assertIsNone(self.recorder.selector_overlay)

    def test_closing_overlay_restores_main_window(self):
        self.recorder.showMinimized()
        self.recorder.select_btn.click()
        self.app.processEvents()
        selector = self.recorder.selector_overlay

        selector.close()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

        self.assertIsNone(self.recorder.selector_overlay)
        self.assertTrue(self.recorder.isVisible())
        self.assertFalse(self.recorder.isMinimized())
        self.assertTrue(self.recorder.select_btn.isEnabled())

    def test_selection_callback_error_restores_main_window(self):
        self.recorder.showMinimized()
        with patch("screen_recorder.BorderWindow", side_effect=RuntimeError("border failed")):
            self.recorder.select_btn.click()
            self.app.processEvents()
            selector = self.recorder.selector_overlay
            selector.selection_made.emit(10, 20, 100, 100)
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            self.app.processEvents()

        self.assertIsNone(self.recorder.selector_overlay)
        self.assertTrue(self.recorder.isVisible())
        self.assertFalse(self.recorder.isMinimized())
        self.assertTrue(self.recorder.select_btn.isEnabled())

    def test_audio_start_error_keeps_app_open_and_allows_retry(self):
        self.recorder.recording_rect = (10, 20, 100, 100)
        self.recorder.record_btn.setEnabled(True)
        failed_session = FakeAudioSession(fail=True)

        with patch("screen_recorder.AudioSession", return_value=failed_session):
            self.recorder.record_btn.click()

        self.assertTrue(self.recorder.isVisible())
        self.assertFalse(self.recorder.recording)
        self.assertTrue(self.recorder.record_btn.isEnabled())
        self.assertTrue(self.recorder.audio_combo.isEnabled())

        working_session = FakeAudioSession()
        with patch("screen_recorder.AudioSession", return_value=working_session):
            self.recorder.record_btn.click()

        self.assertTrue(working_session.started)
        self.assertTrue(self.recorder.recording)
        worker = self.recorder.recorder_thread
        self.recorder.stop_btn.click()
        worker.complete()
        self.app.processEvents()
        self.assertFalse(self.recorder.recording)

    def test_recording_mode_replaces_duplicate_fps_and_quality_controls(self):
        self.assertFalse(hasattr(self.recorder, "fps_spin"))
        self.assertFalse(hasattr(self.recorder, "quality_combo"))
        self.assertEqual(
            [self.recorder.recording_mode_combo.itemData(index)
             for index in range(self.recorder.recording_mode_combo.count())],
            [profile.key for profile in VIDEO_PROFILES],
        )
        self.assertEqual(
            [self.recorder.recording_mode_combo.itemText(index)
             for index in range(self.recorder.recording_mode_combo.count())],
            [
                translate("ru", translation_key)
                for translation_key, _ in RECORDING_MODES
            ],
        )
        self.assertTrue(self.recorder.open_folder_btn.isEnabled())
        self.assertLessEqual(self.recorder.minimumSizeHint().width(), 800)

    def test_approved_icon_is_available_to_the_main_window(self):
        self.assertTrue(bundled_resource_path("screen-recorder-icon.ico").is_file())
        self.assertFalse(self.recorder.windowIcon().isNull())

    def test_recording_controls_disable_and_restore_the_mode_selector(self):
        self.recorder.recording_rect = (10, 20, 100, 100)
        self.recorder.record_btn.setEnabled(True)
        session = FakeAudioSession()

        with patch("screen_recorder.AudioSession", return_value=session):
            self.recorder.record_btn.click()

        self.assertFalse(self.recorder.recording_mode_combo.isEnabled())
        worker = self.recorder.recorder_thread
        self.recorder.stop_btn.click()
        worker.complete()
        self.app.processEvents()
        self.assertTrue(self.recorder.recording_mode_combo.isEnabled())

    def test_pause_and_resume_update_video_and_audio_state(self):
        self.recorder.recording_rect = (10, 20, 100, 100)
        self.recorder.record_btn.setEnabled(True)
        session = FakeAudioSession()

        with patch("screen_recorder.AudioSession", return_value=session):
            self.recorder.record_btn.click()

        worker = self.recorder.recorder_thread
        self.recorder.pause_btn.click()
        self.assertTrue(worker.is_paused)
        self.assertEqual(session.paused_states, [True])
        self.assertEqual(self.recorder.pause_btn.text(), "Продолжить")

        self.recorder.pause_btn.click()
        self.assertFalse(worker.is_paused)
        self.assertEqual(session.paused_states, [True, False])
        self.assertEqual(self.recorder.pause_btn.text(), "Пауза")

    def test_failed_finalization_preserves_temporary_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "video.mkv"
            final_path = Path(temp_dir) / "video.mp4"
            raw_path.write_bytes(b"video")
            session = FakeAudioSession()
            self.recorder.audio_session = session
            self.recorder.recorder_thread = SimpleNamespace(
                output_path=raw_path,
                frame_count=15,
                effective_fps=15,
                active_recording_seconds=1.0,
                isRunning=lambda: False,
            )
            self.recorder.final_output_path = final_path

            with patch(
                "screen_recorder.mux_recording",
                side_effect=MediaMuxError("mux failed"),
            ):
                self.recorder.on_recording_finished()

            self.assertTrue(raw_path.exists())
            self.assertFalse(final_path.exists())
            self.assertFalse(session.cleaned)

    def test_successful_finalization_removes_current_temporary_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path = Path(temp_dir) / "video.mkv"
            final_path = Path(temp_dir) / "video.mp4"
            raw_path.write_bytes(b"video")
            session = FakeAudioSession()
            self.recorder.audio_session = session
            self.recorder.recorder_thread = SimpleNamespace(
                output_path=raw_path,
                frame_count=15,
                effective_fps=15,
                active_recording_seconds=1.0,
                isRunning=lambda: False,
            )
            self.recorder.final_output_path = final_path

            def fake_mux(*args, **kwargs):
                final_path.write_bytes(b"mp4")

            with patch(
                "screen_recorder.mux_recording",
                side_effect=fake_mux,
            ):
                self.recorder.on_recording_finished()

            self.assertFalse(raw_path.exists())
            self.assertTrue(final_path.exists())
            self.assertTrue(session.cleaned)

    def test_open_recordings_folder_uses_portable_recordings_directory(self):
        with patch("screen_recorder.recordings_directory") as directory_mock, patch(
            "screen_recorder.os.startfile", create=True
        ) as startfile_mock:
            directory = directory_mock.return_value
            self.recorder.open_recordings_folder()

        directory.mkdir.assert_called_once_with(parents=True, exist_ok=True)
        startfile_mock.assert_called_once_with(str(directory))


if __name__ == "__main__":
    unittest.main()
