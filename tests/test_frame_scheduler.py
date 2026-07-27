import unittest
from unittest.mock import patch

import numpy as np

from frame_scheduler import FrameScheduler
from screen_recorder import RecorderThread


class FrameSchedulerTests(unittest.TestCase):
    def test_intervals_for_supported_frame_rates(self):
        for fps in (15, 24, 30):
            with self.subTest(fps=fps):
                scheduler = FrameScheduler(fps, 10.0)
                self.assertAlmostEqual(scheduler.next_deadline(), 10.0)
                scheduler.mark_captured()
                self.assertAlmostEqual(
                    scheduler.next_deadline(), 10.0 + 1.0 / fps, places=9
                )

    def test_deadlines_do_not_accumulate_drift(self):
        scheduler = FrameScheduler(30, 100.0)
        for frame_index in range(1, 301):
            scheduler.mark_captured()
            self.assertAlmostEqual(
                scheduler.next_deadline(),
                100.0 + frame_index / 30.0,
                places=9,
            )

    def test_missed_deadline_does_not_change_timeline_rate(self):
        scheduler = FrameScheduler(30, 0.0)
        scheduler.mark_captured()
        repeated_frames = scheduler.claim_due_frames(0.12)

        self.assertEqual(repeated_frames, 4)
        self.assertEqual(scheduler.missed_deadlines, 3)
        self.assertAlmostEqual(scheduler.next_deadline(), 1.0 / 30.0)
        self.assertEqual(scheduler.wait_seconds(0.12), 0.0)
        scheduler.mark_captured()
        self.assertEqual(scheduler.claim_due_frames(0.121), 0)
        self.assertAlmostEqual(
            scheduler.output_wait_seconds(0.121),
            4.0 / 30.0 - 0.121,
        )
        self.assertEqual(scheduler.claim_due_frames(4.0 / 30.0), 1)

    def test_pause_is_excluded_from_active_timeline(self):
        scheduler = FrameScheduler(24, 10.0)
        scheduler.pause(12.0)
        self.assertAlmostEqual(scheduler.active_elapsed(15.0), 2.0)

        scheduler.resume(17.0)
        self.assertAlmostEqual(scheduler.active_elapsed(18.0), 3.0)
        self.assertAlmostEqual(scheduler.total_paused, 5.0)

    def test_final_duration_tracks_active_wall_clock(self):
        for fps in (15, 24, 30):
            with self.subTest(fps=fps):
                scheduler = FrameScheduler(fps, 20.0)
                active_seconds = 7.25
                frames = scheduler.final_frame_count(20.0 + active_seconds)
                file_duration = frames / fps
                self.assertLessEqual(
                    abs(file_duration - active_seconds), 0.5 / fps
                )


class FakeMssSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeVideoWriter:
    def __init__(self):
        self.released = False
        self.frames = 0

    def isOpened(self):
        return True

    def write(self, frame):
        self.frames += 1

    def release(self):
        self.released = True


class RecorderResourceTests(unittest.TestCase):
    def test_stop_releases_capture_and_writer_resources(self):
        worker = RecorderThread((0, 0, 100, 100), "resource_test.mp4", 30)
        session = FakeMssSession()
        writer = FakeVideoWriter()

        def capture_once(*args):
            worker.is_recording = False
            return np.zeros((100, 100, 3), dtype=np.uint8)

        with patch("screen_recorder.mss.mss", return_value=session), patch.object(
            worker, "_capture_frame", side_effect=capture_once
        ), patch.object(worker, "_draw_cursor_and_clicks"), patch(
            "screen_recorder.cv2.VideoWriter", return_value=writer
        ), patch("screen_recorder.cv2.VideoWriter_fourcc", return_value=0):
            worker.run()

        self.assertTrue(writer.released)
        self.assertTrue(session.closed)
        self.assertIsNone(worker.video_writer)
        self.assertFalse(worker.is_recording)
        self.assertEqual(worker.frames, [])


if __name__ == "__main__":
    unittest.main()
