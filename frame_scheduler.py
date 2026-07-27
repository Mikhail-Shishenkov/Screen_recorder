import math


class FrameScheduler:
    """Fixed-rate frame timeline based on active monotonic time."""

    def __init__(self, fps, started_at):
        if fps <= 0:
            raise ValueError("fps must be positive")
        self.fps = float(fps)
        self.frame_period = 1.0 / self.fps
        self.started_at = float(started_at)
        self.total_paused = 0.0
        self.paused_at = None
        self.captured_frames = 0
        self.output_frames = 0
        self.missed_deadlines = 0

    @property
    def is_paused(self):
        return self.paused_at is not None

    def pause(self, now):
        if self.paused_at is None:
            self.paused_at = float(now)

    def resume(self, now):
        if self.paused_at is None:
            return
        self.total_paused += max(0.0, float(now) - self.paused_at)
        self.paused_at = None

    def active_elapsed(self, now):
        effective_now = self.paused_at if self.paused_at is not None else float(now)
        return max(0.0, effective_now - self.started_at - self.total_paused)

    def next_deadline(self):
        return (
            self.started_at
            + self.total_paused
            + self.captured_frames * self.frame_period
        )

    def wait_seconds(self, now):
        if self.is_paused:
            return 0.0
        return max(0.0, self.next_deadline() - float(now))

    def output_wait_seconds(self, now):
        deadline = (
            self.started_at
            + self.total_paused
            + self.output_frames * self.frame_period
        )
        return max(0.0, deadline - float(now))

    def mark_captured(self):
        self.captured_frames += 1

    def claim_due_frames(self, now):
        elapsed = self.active_elapsed(now)
        expected_frames = max(1, math.floor(elapsed * self.fps + 1e-9) + 1)
        due_frames = max(0, expected_frames - self.output_frames)
        self.output_frames += due_frames
        self.missed_deadlines += max(0, due_frames - 1)
        return due_frames

    def final_frame_count(self, now):
        return max(1, round(self.active_elapsed(now) * self.fps))
