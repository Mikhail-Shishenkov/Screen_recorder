import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtWidgets import QApplication

from screen_recorder import ScreenRecorder


class FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in tuple(self._slots):
            slot(*args)


class FakeRecorderThread:
    def __init__(self, bbox, output_path, fps):
        self.bbox = bbox
        self.output_path = output_path
        self.fps = fps
        self.is_recording = True
        self.is_paused = False
        self.finished = FakeSignal()
        self.error = FakeSignal()
        self._running = False

    def start(self):
        self._running = True

    def isRunning(self):
        return self._running

    def complete(self):
        self.is_recording = False
        self._running = False
        self.finished.emit()


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
        self.recorder = ScreenRecorder()
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


if __name__ == "__main__":
    unittest.main()
