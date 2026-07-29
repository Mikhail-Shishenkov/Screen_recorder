import time
from datetime import datetime
from pathlib import Path
import sys
import os
import pyautogui
import ctypes
import traceback


from PIL import ImageGrab
import cv2
import mss
import numpy as np
from PyQt5.QtGui import QFont, QIcon, QMovie
from PyQt5.QtWidgets import QDialog
from region_geometry import (
    normalize_selection_rect,
    rect_to_capture_bbox,
    rect_to_capture_region,
)
from frame_scheduler import FrameScheduler
from audio_capture import AUDIO_MODE_LABELS, AudioCaptureError, AudioSession
from ffmpeg_video_writer import (
    FFmpegVideoError,
    FFmpegVideoWriter,
    VIDEO_PROFILES,
    get_video_profile,
)
from media_mux import MediaMuxError, mux_recording
from localization import (
    DEFAULT_LANGUAGE,
    LANGUAGE_OPTIONS,
    normalize_language,
    translate,
)


from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QGridLayout, QPushButton, QLabel, QComboBox, QFrame, QSizePolicy
)
from PyQt5.QtCore import pyqtSignal, QThread, Qt, QTimer, QRect, QSettings

import platform
from PyQt5.QtGui import QPainter, QPen, QColor

# === ВКЛ/ВЫКЛ логов в терминал ===
DEBUG = False

RECORDING_MODES = tuple(
    (f"quality.{profile.key}", profile.key)
    for profile in VIDEO_PROFILES
)

AUDIO_MODE_TRANSLATION_KEYS = {
    "off": "audio.off",
    "system": "audio.system",
    "microphone": "audio.microphone",
    "system_microphone": "audio.system_microphone",
}

def debug_print(*args, **kwargs):
    if DEBUG:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe_args = [
            str(arg).encode(encoding, errors="replace").decode(encoding)
            for arg in args
        ]
        print(*safe_args, **kwargs)


def application_directory():
    """Return the directory containing the executable or source entrypoint."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def recordings_directory():
    """Keep user recordings next to the portable application."""
    return application_directory() / "Мои записи"


def bundled_resource_path(filename):
    """Resolve a resource in development and a PyInstaller onefile bundle."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return Path(bundle_dir) / filename

    root = application_directory()
    direct_path = root / filename
    if direct_path.is_file():
        return direct_path
    return root / "assets" / filename


def enable_dpi_awareness():
    """Make the process per-monitor DPI aware before QApplication exists."""
    if not sys.platform.startswith("win"):
        return

    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass


def set_windows_app_user_model_id():
    """Associate the process with the packaged Screen Recorder identity on Windows."""
    if not sys.platform.startswith("win"):
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ScreenRecorderPro.Portable"
        )
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_virtual_screen_geometry():
    """Return the virtual desktop bounds in physical screen coordinates."""
    if sys.platform.startswith("win"):
        try:
            user32 = ctypes.windll.user32
            return QRect(
                user32.GetSystemMetrics(76),
                user32.GetSystemMetrics(77),
                user32.GetSystemMetrics(78),
                user32.GetSystemMetrics(79),
            )
        except Exception:
            pass

    app = QApplication.instance()
    if app:
        geometry = QRect()
        for screen in app.screens():
            geometry = geometry.united(screen.geometry())
        if not geometry.isNull():
            return geometry

    primary_screen = QApplication.primaryScreen()
    if primary_screen:
        return primary_screen.geometry()
    return QRect(0, 0, 0, 0)


# ===== RAMKA ZAPISI (PyQt-окошко поверх всего) =====
class BorderWindow(QWidget):
    def __init__(self, bbox, parent=None):
        super().__init__(parent)
        self.bbox = bbox

        # Полностью прозрачный фон, только рамка
        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        x, y, w, h = self.bbox
        self.setGeometry(x, y, w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        pen = QPen(QColor(255, 0, 0), 3)  # красная рамка
        painter.setPen(pen)
        # Чуть отступаем, чтобы линия не "съедалась"
        painter.drawRect(1, 1, self.width() - 2, self.height() - 2)


class RecorderThread(QThread):
    completed = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, bbox, output_path, profile_key="maximum"):
        super().__init__()
        self.bbox = bbox  # (x, y, width, height)
        self.output_path = output_path
        self.video_profile = get_video_profile(profile_key)
        self.fps = self.video_profile.fps
        self.effective_fps = self.video_profile.fps
        self.is_recording = True
        self.is_paused = False

        # frames оставляем для совместимости, но не используем
        self.frames = []
        self.frame_count = 0
        self.captured_frame_count = 0
        self.missed_frame_count = 0
        self.active_recording_seconds = 0.0
        self.prev_left_down = False
        self.prev_right_down = False
        self.click_anim_left = 0
        self.click_anim_right = 0

        # Определяем, нужен ли fallback для захвата на Windows 7
        self.use_pyautogui_capture = False
        if sys.platform.startswith('win'):
            try:
                if platform.release() == '7':
                    self.use_pyautogui_capture = True
            except Exception:
                self.use_pyautogui_capture = False

        self.video_writer = None

    # -------- ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ЗАХВАТА --------
    def _grab_pil_frame(self, capture_bbox):
        grab_kwargs = {"bbox": capture_bbox}
        if sys.platform.startswith("win"):
            grab_kwargs["all_screens"] = True
        return ImageGrab.grab(**grab_kwargs)

    def _capture_frame_raw(self, capture_bbox, capture_region):
        """Захват одного кадра без курсора и анимаций."""
        if self.use_pyautogui_capture:
            screenshot = pyautogui.screenshot(region=capture_region)
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        else:
            screenshot = self._grab_pil_frame(capture_bbox)
            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
        return frame

    def _capture_frame(self, capture_bbox, capture_region, capture_session):
        if self.use_pyautogui_capture:
            screenshot = pyautogui.screenshot(region=capture_region)
            return cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_RGB2BGR)

        left, top, right, bottom = capture_bbox
        monitor = {
            "left": left,
            "top": top,
            "width": right - left,
            "height": bottom - top,
        }
        screenshot = capture_session.grab(monitor)
        return cv2.cvtColor(np.asarray(screenshot), cv2.COLOR_BGRA2BGR)

    def _draw_cursor_and_clicks(self, frame, x, y, w, h):
        mouse_x, mouse_y = pyautogui.position()
        rel_x = mouse_x - x
        rel_y = mouse_y - y
        if not (0 <= rel_x < w and 0 <= rel_y < h):
            return

        user32 = ctypes.windll.user32
        left_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        right_down = bool(user32.GetAsyncKeyState(0x02) & 0x8000)
        if left_down and not self.prev_left_down:
            self.click_anim_left = 10
        if right_down and not self.prev_right_down:
            self.click_anim_right = 10
        self.prev_left_down = left_down
        self.prev_right_down = right_down

        cx, cy = int(rel_x), int(rel_y)
        pts = np.array([
            [cx, cy],
            [cx + 10, cy + 25],
            [cx + 4, cy + 18],
            [cx - 6, cy + 22],
        ], np.int32)
        cv2.fillConvexPoly(frame, pts, (255, 255, 255))
        cv2.polylines(frame, [pts], True, (0, 0, 0), 1)

        if self.click_anim_left > 0:
            radius = 20 + (10 - self.click_anim_left) * 2
            cv2.circle(frame, (cx, cy), radius, (0, 0, 255), 2)
            self.click_anim_left -= 1
        if self.click_anim_right > 0:
            radius = 20 + (10 - self.click_anim_right) * 2
            cv2.circle(frame, (cx, cy), radius, (255, 0, 0), 2)
            self.click_anim_right -= 1

    @staticmethod
    def _timing_summary(values):
        if not values:
            return "avg=0.00ms min=0.00ms p95=0.00ms"
        ordered = sorted(values)
        p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return (
            f"avg={sum(values) / len(values) * 1000.0:.2f}ms "
            f"min={ordered[0] * 1000.0:.2f}ms "
            f"p95={ordered[p95_index] * 1000.0:.2f}ms"
        )

    def run(self):
        frame_writer = None
        capture_session = None
        scheduler = None
        last_frame = None
        recording_succeeded = False
        timings = {
            "capture": [],
            "cursor_and_clicks": [],
            "write": [],
            "wait": [],
            "frame": [],
        }
        try:
            debug_print(f"Recording started: {self.output_path}")
            debug_print(f"Capture rectangle: {self.bbox}")

            x, y, w, h = self.bbox
            capture_bbox = rect_to_capture_bbox(self.bbox)
            capture_region = rect_to_capture_region(self.bbox)
            if not self.use_pyautogui_capture:
                capture_session = mss.mss()

            self.effective_fps = float(self.fps)
            started_at = time.perf_counter()
            scheduler = FrameScheduler(self.effective_fps, started_at)
            debug_print(f"Target FPS: {self.effective_fps:.1f}")

            while self.is_recording:
                now = time.perf_counter()
                if self.is_paused:
                    scheduler.pause(now)
                    time.sleep(0.01)
                    continue
                if scheduler.is_paused:
                    scheduler.resume(now)

                wait_time = scheduler.wait_seconds(now)
                if wait_time > 0:
                    wait_started = time.perf_counter()
                    time.sleep(wait_time)
                    timings["wait"].append(time.perf_counter() - wait_started)
                    continue

                frame_started = time.perf_counter()
                try:
                    capture_started = time.perf_counter()
                    frame = self._capture_frame(
                        capture_bbox, capture_region, capture_session
                    )
                    timings["capture"].append(time.perf_counter() - capture_started)
                    self.captured_frame_count += 1
                    scheduler.mark_captured()

                    overlay_started = time.perf_counter()
                    self._draw_cursor_and_clicks(frame, x, y, w, h)
                    timings["cursor_and_clicks"].append(
                        time.perf_counter() - overlay_started
                    )

                    if frame_writer is None:
                        h_frame, w_frame = frame.shape[:2]
                        frame_writer = FFmpegVideoWriter(
                            self.output_path,
                            w_frame,
                            h_frame,
                            self.video_profile,
                            logger=debug_print,
                        )
                        self.video_writer = frame_writer

                    due_frames = scheduler.claim_due_frames(time.perf_counter())
                    if due_frames == 0:
                        output_wait = scheduler.output_wait_seconds(
                            time.perf_counter()
                        )
                        if output_wait:
                            time.sleep(output_wait)
                        due_frames = scheduler.claim_due_frames(
                            time.perf_counter()
                        )
                    write_started = time.perf_counter()
                    if due_frames:
                        frame_writer.submit(frame, due_frames)
                    timings["write"].append(time.perf_counter() - write_started)
                    self.frame_count += due_frames
                    self.missed_frame_count += max(0, due_frames - 1)
                    last_frame = frame
                    timings["frame"].append(time.perf_counter() - frame_started)
                except FFmpegVideoError:
                    raise
                except Exception as exc:
                    debug_print(f"Capture error: {exc}")
                    time.sleep(0.01)

            stopped_at = time.perf_counter()
            self.active_recording_seconds = scheduler.active_elapsed(stopped_at)
            target_frames = scheduler.final_frame_count(stopped_at)
            if frame_writer is not None and last_frame is not None:
                write_started = time.perf_counter()
                missing_frames = max(0, target_frames - self.frame_count)
                if missing_frames:
                    frame_writer.submit(last_frame, missing_frames)
                    self.frame_count += missing_frames
                    self.missed_frame_count += missing_frames
                timings["write"].append(time.perf_counter() - write_started)

            debug_print("Capture finished")
            debug_print(f"Captured frames: {self.captured_frame_count}")
            debug_print(f"Output frames: {self.frame_count}")
            debug_print(f"Missed deadlines: {self.missed_frame_count}")
            debug_print(
                f"Active wall time: {self.active_recording_seconds:.3f}s"
            )
            actual_fps = (
                self.captured_frame_count / self.active_recording_seconds
                if self.active_recording_seconds > 0
                else 0.0
            )
            debug_print(f"Actual capture FPS: {actual_fps:.2f}")
            for stage, values in timings.items():
                debug_print(f"Timing {stage}: {self._timing_summary(values)}")

            if frame_writer is not None:
                frame_writer.close()
                frame_writer = None
                self.video_writer = None

            if self.frame_count == 0:
                raise Exception("Не было захвачено ни одного кадра")

            output_path_str = str(self.output_path)
            if Path(output_path_str).exists():
                file_size = Path(output_path_str).stat().st_size / (1024 * 1024)
                debug_print(
                    f"Video saved: size={file_size:.1f} MB; "
                    f"frames={self.frame_count}; FPS={self.effective_fps:.1f}"
                )
            else:
                debug_print("Video file was not found after recording")
            recording_succeeded = True

        except Exception as e:
            error_msg = f"Ошибка при записи видео: {str(e)}"
            debug_print(error_msg)
            self.error.emit(error_msg)
        finally:
            if frame_writer is not None:
                try:
                    frame_writer.abort()
                except Exception as exc:
                    debug_print(f"Frame writer close error: {exc}")
            self.video_writer = None
            if capture_session is not None:
                capture_session.close()
            self.frames = []
            self.is_recording = False
            if recording_succeeded:
                self.completed.emit()


class RegionSelectorTkinter:
    """Простой выбор области с Tkinter"""

    def __init__(self, callback):
        import tkinter as tk

        self.callback = callback
        self.root = tk.Tk()
        self.root.attributes('-alpha', 0.3)
        self.root.attributes('-topmost', True)

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        self.root.geometry(f"{screen_width}x{screen_height}+0+0")

        self.canvas = tk.Canvas(self.root, bg='black', cursor='crosshair', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)

        self.start_x = 0
        self.start_y = 0
        self.rect = None
        self.text_id = None

        self.canvas.bind('<Button-1>', self.on_press)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)
        self.root.bind('<Escape>', lambda e: self.root.destroy())

    def on_press(self, event):
        self.start_x = event.x
        self.start_y = event.y

    def on_drag(self, event):
        if self.rect:
            self.canvas.delete(self.rect)
        if self.text_id:
            self.canvas.delete(self.text_id)

        w = abs(event.x - self.start_x)
        h = abs(event.y - self.start_y)

        self.rect = self.canvas.create_rectangle(
            self.start_x, self.start_y, event.x, event.y,
            outline='lime', width=2
        )

        self.text_id = self.canvas.create_text(
            self.start_x + 10, self.start_y - 10,
            text=f'{w}×{h}', fill='white', font=('Arial', 10, 'bold')
        )

    def on_release(self, event):
        x = min(self.start_x, event.x)
        y = min(self.start_y, event.y)
        w = abs(event.x - self.start_x)
        h = abs(event.y - self.start_y)

        if w < 50 or h < 50:
            self.root.destroy()
            return

        self.callback(x, y, w, h)
        self.root.destroy()

    def show(self):
        self.root.mainloop()

class RegionSelectorOverlay(QWidget):
    selection_made = pyqtSignal(int, int, int, int)
    selection_canceled = pyqtSignal()

    def __init__(self, parent=None, messages=None):
        super().__init__(parent)
        messages = messages or {}
        self._message_select = messages.get(
            "select",
            "Drag to select an area. Esc cancels.",
        )
        self._message_release = messages.get(
            "release",
            "Release to confirm. Esc cancels.",
        )
        self._message_too_small = messages.get(
            "too_small",
            "Area must be at least 2x2 px after even rounding.",
        )
        self.virtual_geometry = get_virtual_screen_geometry()
        self.start_point = None
        self.current_point = None
        self.message = self._message_select
        self._completion_emitted = False
        self._closing = False

        self.setWindowFlags(
            Qt.FramelessWindowHint |
            Qt.WindowStaysOnTopHint |
            Qt.Tool
        )
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(self.virtual_geometry)

    def show_overlay(self):
        self.show()
        self.raise_()
        self.activateWindow()
        self.grabKeyboard()

    def closeEvent(self, event):
        self._closing = True
        if self.keyboardGrabber() is self:
            self.releaseKeyboard()
        if not self._completion_emitted:
            self._completion_emitted = True
            self.selection_canceled.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._completion_emitted = True
            self.selection_canceled.emit()
            self.close()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.start_point = event.globalPos()
        self.current_point = event.globalPos()
        self.message = self._message_release
        self.update()

    def mouseMoveEvent(self, event):
        if self.start_point is None:
            return
        self.current_point = event.globalPos()
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self.start_point is None:
            return

        self.current_point = event.globalPos()
        rect = normalize_selection_rect(
            self.start_point.x(),
            self.start_point.y(),
            self.current_point.x(),
            self.current_point.y(),
        )
        if rect is None:
            self.start_point = None
            self.current_point = None
            self.message = self._message_too_small
            self.update()
            return

        self._completion_emitted = True
        self.selection_made.emit(*rect)
        self.close()

    def _raw_preview_rect(self):
        if self.start_point is None or self.current_point is None:
            return None
        x1 = self.start_point.x()
        y1 = self.start_point.y()
        x2 = self.current_point.x()
        y2 = self.current_point.y()
        return min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1)

    def _preview_rect(self):
        if self.start_point is None or self.current_point is None:
            return None
        normalized = normalize_selection_rect(
            self.start_point.x(),
            self.start_point.y(),
            self.current_point.x(),
            self.current_point.y(),
        )
        return normalized or self._raw_preview_rect()

    def _to_local_qrect(self, rect):
        x, y, width, height = rect
        return QRect(
            x - self.virtual_geometry.x(),
            y - self.virtual_geometry.y(),
            width,
            height,
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 96))

        preview_rect = self._preview_rect()
        if preview_rect:
            local_rect = self._to_local_qrect(preview_rect)
            painter.fillRect(local_rect, QColor(255, 255, 255, 24))
            painter.setPen(QPen(QColor(0, 255, 0), 2))
            painter.drawRect(local_rect.adjusted(0, 0, -1, -1))

            label_width = max(140, local_rect.width())
            label_rect = QRect(
                local_rect.x(),
                max(0, local_rect.y() - 28),
                label_width,
                24,
            )
            painter.fillRect(label_rect, QColor(0, 0, 0, 180))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(
                label_rect.adjusted(8, 0, -8, 0),
                Qt.AlignVCenter | Qt.AlignLeft,
                f"{preview_rect[2]}x{preview_rect[3]}",
            )

        if self.message:
            message_rect = QRect(20, 20, max(320, self.width() - 40), 24)
            painter.fillRect(message_rect, QColor(0, 0, 0, 180))
            painter.setPen(QColor(255, 255, 255))
            painter.drawText(
                message_rect.adjusted(8, 0, -8, 0),
                Qt.AlignVCenter | Qt.AlignLeft,
                self.message,
            )


class HelpWindow(QDialog):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(400, 400)

        layout = QVBoxLayout(self)

        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.label)

        gif_path = str(bundled_resource_path("spider-man-dance.gif"))

        self.movie = QMovie(gif_path)
        self.label.setMovie(self.movie)
        self.movie.start()

class ScreenRecorder(QMainWindow):
    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings or QSettings(
            "ScreenRecorderPro",
            "ScreenRecorderPro",
        )
        stored_language = self.settings.value(
            "ui/language",
            DEFAULT_LANGUAGE,
        )
        self.current_language = normalize_language(stored_language)
        self.setWindowTitle(self._t("app.title"))
        app_font = QFont("Segoe UI")
        app_font.setPointSizeF(10.5)
        self.setFont(app_font)

        self.recording = False
        self.paused = False
        self.recorder_thread = None
        self.recording_rect = None
        self.border_window = None
        self.selector_overlay = None
        self.keyboard = None  # модуль для глобальных хоткеев
        self.audio_session = None
        self.hotkeys_available = True
        self._status_state = "select"
        self._status_title_key = "status.select.title"
        self._status_detail_key = "status.select.initial"
        self._status_values = {}

        self._build_approved_ui()
        self.init_hotkeys()

    # === ГОРЯЧИЕ КЛАВИШИ (глобальные через модуль keyboard) ===
    def _build_approved_ui(self):
        self.resize(840, 600)
        self.setMinimumSize(760, 560)
        self.setWindowIcon(QIcon(str(bundled_resource_path("screen-recorder-icon.ico"))))

        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(30, 26, 30, 22)
        layout.setSpacing(18)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.title_label = QLabel()
        self.title_label.setObjectName("appTitle")
        self.title_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("appSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        heading.addWidget(self.title_label)
        heading.addWidget(self.subtitle_label)
        header.addLayout(heading, 1)
        self.language_combo = QComboBox()
        self.language_combo.setObjectName("languageCombo")
        self.language_combo.setSizePolicy(
            QSizePolicy.Fixed,
            QSizePolicy.Fixed,
        )
        self.language_combo.setFixedWidth(120)
        for language, native_name in LANGUAGE_OPTIONS:
            self.language_combo.addItem(native_name, language)
        language_index = self.language_combo.findData(self.current_language)
        self.language_combo.setCurrentIndex(max(0, language_index))
        self.language_combo.currentIndexChanged.connect(
            self._on_language_changed
        )
        header.addWidget(self.language_combo)
        self.help_btn = QPushButton("?")
        self.help_btn.setObjectName("helpButton")
        self.help_btn.clicked.connect(self.show_help)
        header.addWidget(self.help_btn)
        layout.addLayout(header)

        self.status_card = QFrame()
        self.status_card.setObjectName("statusCard")
        self.status_card.setMinimumHeight(92)
        status_layout = QHBoxLayout(self.status_card)
        status_layout.setContentsMargins(18, 16, 18, 16)
        status_layout.setSpacing(16)
        self.status_icon = QLabel("1")
        self.status_icon.setObjectName("statusIcon")
        self.status_icon.setAlignment(Qt.AlignCenter)
        status_layout.addWidget(self.status_icon)
        status_text = QVBoxLayout()
        status_text.setSpacing(4)
        self.status_title = QLabel()
        self.status_title.setObjectName("statusTitle")
        self.status_title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.status_detail = QLabel()
        self.status_detail.setObjectName("statusDetail")
        self.status_detail.setWordWrap(True)
        self.status_detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        status_text.addWidget(self.status_title)
        status_text.addWidget(self.status_detail)
        status_layout.addLayout(status_text, 1)
        self.select_btn = QPushButton()
        self.select_btn.setObjectName("secondaryButton")
        self.select_btn.clicked.connect(self.select_region)
        status_layout.addWidget(self.select_btn)
        layout.addWidget(self.status_card)

        settings_card = QFrame()
        settings_card.setObjectName("settingsCard")
        settings_card.setMinimumHeight(128)
        settings = QGridLayout(settings_card)
        settings.setContentsMargins(20, 18, 20, 20)
        settings.setHorizontalSpacing(18)
        settings.setVerticalSpacing(9)
        settings.setColumnStretch(0, 1)
        settings.setColumnStretch(1, 1)
        self.mode_label = QLabel()
        self.mode_label.setObjectName("fieldLabel")
        self.sound_label = QLabel()
        self.sound_label.setObjectName("fieldLabel")
        settings.addWidget(self.mode_label, 0, 0)
        settings.addWidget(self.sound_label, 0, 1)
        self.recording_mode_combo = QComboBox()
        self.recording_mode_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for translation_key, profile_key in RECORDING_MODES:
            self.recording_mode_combo.addItem(
                self._t(translation_key),
                profile_key,
            )
        settings.addWidget(self.recording_mode_combo, 1, 0)
        self.audio_combo = QComboBox()
        self.audio_combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for _, mode in AUDIO_MODE_LABELS:
            self.audio_combo.addItem(
                self._t(AUDIO_MODE_TRANSLATION_KEYS[mode]),
                mode,
            )
        settings.addWidget(self.audio_combo, 1, 1)
        layout.addWidget(settings_card)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 2, 0, 0)
        actions.setSpacing(12)
        self.record_btn = QPushButton()
        self.record_btn.setObjectName("recordButton")
        self.record_btn.setMinimumWidth(178)
        self.record_btn.clicked.connect(self.start_recording)
        self.record_btn.setEnabled(False)
        actions.addWidget(self.record_btn)
        self.pause_btn = QPushButton()
        self.pause_btn.setObjectName("secondaryButton")
        self.pause_btn.clicked.connect(self.toggle_pause)
        actions.addWidget(self.pause_btn)
        self.stop_btn = QPushButton()
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.clicked.connect(self.stop_recording)
        actions.addWidget(self.stop_btn)
        actions.addStretch()
        self.open_folder_btn = QPushButton()
        self.open_folder_btn.setObjectName("quietButton")
        self.open_folder_btn.clicked.connect(self.open_recordings_folder)
        actions.addWidget(self.open_folder_btn)
        layout.addLayout(actions)

        self.shortcuts_label = QLabel()
        self.shortcuts_label.setObjectName("shortcuts")
        self.shortcuts_label.setWordWrap(True)
        self.shortcuts_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(self.shortcuts_label)

        layout.addStretch(1)
        footer_row = QFrame()
        footer_row.setObjectName("footerRow")
        footer_layout = QHBoxLayout(footer_row)
        footer_layout.setContentsMargins(0, 13, 0, 0)
        footer_layout.setSpacing(10)
        footer_accent = QLabel("◆")
        footer_accent.setObjectName("footerAccent")
        footer_layout.addWidget(footer_accent, 0, Qt.AlignTop)
        self.footer_label = QLabel()
        self.footer_label.setObjectName("footer")
        self.footer_label.setWordWrap(True)
        footer_layout.addWidget(self.footer_label, 1)
        layout.addWidget(footer_row)

        self.setStyleSheet(self._approved_stylesheet())
        self.apply_language()
        self._set_status(
            "select",
            "status.select.title",
            "status.select.initial",
        )
        self._set_recording_actions(False)

    def _approved_stylesheet(self):
        arrow_path = bundled_resource_path("chevron-down.svg").as_posix()
        return (
            "QMainWindow, QWidget#appRoot { background: #F6F3EE; color: #202B38; }"
            "QLabel { background: transparent; color: #202B38; }"
            "QLabel#appTitle { font-size: 21pt; font-weight: 600; color: #202B38; }"
            "QLabel#appSubtitle, QLabel#statusDetail, QLabel#shortcuts, QLabel#footer { color: #5F6B78; }"
            "QLabel#appSubtitle { font-size: 10.5pt; }"
            "QFrame#statusCard { border: 1px solid #D6E1EB; border-left: 4px solid #2C5B86; border-radius: 12px; background: #EDF3F8; }"
            "QFrame#statusCard[status='recording'] { border-color: #EBC8C5; border-left-color: #B83830; background: #FBEFEE; }"
            "QFrame#statusCard[status='paused'] { border-color: #E6D6BC; border-left-color: #C69450; background: #FBF6EC; }"
            "QFrame#statusCard[status='saved'] { border-color: #CFE3D6; border-left-color: #2E7D5B; background: #EDF6F0; }"
            "QFrame#statusCard[status='error'] { border-color: #EBC8C5; border-left-color: #B83830; background: #FBEFEE; }"
            "QLabel#statusIcon { min-width: 40px; max-width: 40px; min-height: 40px; max-height: 40px; border-radius: 20px; background: #2C5B86; color: white; font-size: 14pt; font-weight: 600; }"
            "QFrame#statusCard[status='recording'] QLabel#statusIcon, QFrame#statusCard[status='error'] QLabel#statusIcon { background: #B83830; }"
            "QFrame#statusCard[status='paused'] QLabel#statusIcon { background: #C69450; }"
            "QFrame#statusCard[status='saved'] QLabel#statusIcon { background: #2E7D5B; }"
            "QLabel#statusTitle { font-size: 12pt; font-weight: 600; }"
            "QLabel#statusDetail { font-size: 10pt; }"
            "QFrame#settingsCard { border: 1px solid #D7D3CC; border-radius: 12px; background: #FFFDF9; }"
            "QLabel#fieldLabel { color: #364454; font-size: 10.5pt; font-weight: 600; }"
            "QLabel#shortcuts { font-size: 10pt; }"
            "QFrame#footerRow { border-top: 1px solid #DEDAD4; background: transparent; }"
            "QLabel#footerAccent { color: #C69450; font-size: 10pt; }"
            "QLabel#footer { color: #5F6B78; font-size: 10pt; }"
            "QPushButton, QComboBox { font-family: 'Segoe UI'; font-size: 10.5pt; }"
            "QComboBox { min-height: 44px; padding: 0 34px 0 13px; border: 1px solid #AEB6BF; border-radius: 9px; background: #FFFFFF; color: #202B38; }"
            "QComboBox#languageCombo { min-height: 40px; max-height: 40px; padding: 0 0 0 9px; font-size: 10pt; }"
            "QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 34px; border: 0; background: transparent; }"
            "QComboBox#languageCombo::drop-down { width: 26px; }"
            f'QComboBox::down-arrow {{ image: url("{arrow_path}"); width: 10px; height: 6px; }}'
            "QComboBox:hover { border-color: #7E8D9C; }"
            "QComboBox:focus { border: 2px solid #2C5B86; }"
            "QComboBox:disabled { background: #EEECE8; color: #8D8A86; border-color: #D7D3CC; }"
            "QComboBox QAbstractItemView { background: #FFFFFF; color: #202B38; border: 1px solid #AEB6BF; selection-background-color: #E5EEF6; selection-color: #202B38; padding: 5px; outline: 0; }"
            "QPushButton { min-height: 44px; padding: 0 18px; border: 1px solid #AAB3BD; border-radius: 10px; background: #FFFFFF; color: #263B52; font-weight: 600; }"
            "QPushButton#recordButton, QPushButton#stopButton { background: #B83830; color: white; border: 1px solid #B83830; }"
            "QPushButton#recordButton:hover, QPushButton#stopButton:hover { background: #A52F29; }"
            "QPushButton#secondaryButton { background: #FFFFFF; color: #263B52; border: 1px solid #AAB3BD; }"
            "QPushButton#secondaryButton:hover { background: #F2F5F7; }"
            "QPushButton#quietButton, QPushButton#helpButton { background: transparent; color: #263B52; border: 1px solid transparent; }"
            "QPushButton#quietButton:hover, QPushButton#helpButton:hover { background: #ECE9E4; }"
            "QPushButton#helpButton { min-width: 44px; max-width: 44px; padding: 0; border-color: #D7D3CC; background: #FFFDF9; }"
            "QPushButton:disabled, QPushButton#recordButton:disabled, QPushButton#stopButton:disabled, "
            "QPushButton#secondaryButton:disabled, QPushButton#quietButton:disabled { "
            "background: #E6E3DE; color: #8D8A86; border-color: #D8D4CE; }"
        )

    def _t(self, key, **values):
        return translate(self.current_language, key, **values)

    def _on_language_changed(self, index):
        self.set_language(self.language_combo.itemData(index))

    def set_language(self, language):
        language = normalize_language(language)
        self.current_language = language
        index = self.language_combo.findData(language)
        if index >= 0 and self.language_combo.currentIndex() != index:
            self.language_combo.blockSignals(True)
            self.language_combo.setCurrentIndex(index)
            self.language_combo.blockSignals(False)
        self.settings.setValue("ui/language", language)
        self.settings.sync()
        self.apply_language()

    def _translate_combo_items(self, combo, key_for_value):
        current_value = combo.currentData()
        combo.blockSignals(True)
        for index in range(combo.count()):
            value = combo.itemData(index)
            combo.setItemText(index, self._t(key_for_value(value)))
        selected_index = combo.findData(current_value)
        if selected_index >= 0:
            combo.setCurrentIndex(selected_index)
        combo.blockSignals(False)

    def apply_language(self):
        self.setWindowTitle(self._t("app.title"))
        self.title_label.setText(self._t("app.title"))
        self.subtitle_label.setText(self._t("app.subtitle"))
        self.language_combo.setToolTip(self._t("language.tooltip"))
        self.help_btn.setToolTip(self._t("help.tooltip"))
        self.mode_label.setText(self._t("field.recording_mode"))
        self.sound_label.setText(self._t("field.audio"))
        self.record_btn.setText(self._t("button.start"))
        self.stop_btn.setText(self._t("button.stop"))
        self.open_folder_btn.setText(self._t("button.open_folder"))
        self.select_btn.setText(
            self._t(
                "button.change_region"
                if self.recording_rect is not None
                else "button.select_region"
            )
        )
        self.pause_btn.setText(
            self._t(
                "button.resume"
                if self.recording and self.paused
                else "button.pause"
            )
        )
        self.shortcuts_label.setText(
            self._t(
                "shortcuts.available"
                if self.hotkeys_available
                else "shortcuts.unavailable"
            )
        )
        self.footer_label.setText(self._t("footer.recordings"))
        self._translate_combo_items(
            self.recording_mode_combo,
            lambda value: f"quality.{value}",
        )
        self._translate_combo_items(
            self.audio_combo,
            lambda value: AUDIO_MODE_TRANSLATION_KEYS[value],
        )
        self._render_status()

    def _render_status(self):
        self.status_title.setText(
            self._t(self._status_title_key, **self._status_values)
        )
        if self._status_detail_key:
            detail = self._t(
                self._status_detail_key,
                **self._status_values,
            )
        else:
            detail = ""
        self.status_detail.setText(detail)

    def _set_status(self, state, title_key, detail_key="", **values):
        self._status_state = state
        self._status_title_key = title_key
        self._status_detail_key = detail_key
        self._status_values = values
        icons = {"select": "1", "ready": "✓", "recording": "●", "paused": "Ⅱ", "saving": "…", "saved": "✓", "error": "!"}
        self.status_card.setProperty("status", state)
        for widget in (self.status_card, self.status_icon):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.status_icon.setText(icons.get(state, "i"))
        self._render_status()

    def _set_recording_actions(self, recording):
        self.select_btn.setVisible(not recording)
        self.record_btn.setVisible(not recording)
        self.pause_btn.setVisible(recording)
        self.stop_btn.setVisible(recording)

    def init_hotkeys(self):
        try:
            import keyboard
            self.keyboard = keyboard

            # Используем QTimer.singleShot, чтобы вызывать методы в GUI-потоке
            # Новые горячие клавиши: Ctrl+1 — запись, Ctrl+2 — пауза, Ctrl+3 — стоп
            self.keyboard.add_hotkey(
                'ctrl+1',
                lambda: QTimer.singleShot(0, self.start_recording)
            )
            self.keyboard.add_hotkey(
                'ctrl+2',
                lambda: QTimer.singleShot(0, self.toggle_pause)
            )
            self.keyboard.add_hotkey(
                'ctrl+3',
                lambda: QTimer.singleShot(0, self.stop_recording)
            )

            debug_print("Глобальные горячие клавиши успешно зарегистрированы")
        except ImportError:
            # Если модуль не установлен – просто работаем без глобальных хоткеев
            self.keyboard = None
            self.hotkeys_available = False
            self.shortcuts_label.setText(self._t("shortcuts.unavailable"))

    def select_region(self):
        self._set_status(
            "select",
            "status.select.title",
            "status.select.overlay",
        )
        self._debug_selection_state("select button handler entered")
        if self.selector_overlay and self.selector_overlay.isVisible():
            self._debug_selection_state("existing selector is still visible")
            return

        if self.selector_overlay:
            self._dispose_selector(self.selector_overlay)

        self.select_btn.setEnabled(False)
        self.record_btn.setEnabled(False)
        self.selector_overlay = RegionSelectorOverlay(
            messages={
                "select": self._t("overlay.select"),
                "release": self._t("overlay.release"),
                "too_small": self._t("overlay.too_small"),
            }
        )
        self.selector_overlay.selection_made.connect(self.on_region_selected)
        self.selector_overlay.selection_canceled.connect(self.on_region_selection_canceled)
        self.selector_overlay.destroyed.connect(self.on_region_selector_closed)
        self._debug_selection_state("selector created")
        self.selector_overlay.show_overlay()
        self._debug_selection_state("selector shown")

    def on_region_selected(self, x, y, w, h):
        selector = self.selector_overlay
        self._debug_selection_state(
            f"selection signal received: QRect({x}, {y}, {w}, {h})"
        )
        try:
            self.recording_rect = (x, y, w, h)
            self._set_status(
                "ready",
                "status.ready.title",
                "status.ready.selected",
                width=w,
                height=h,
            )
            self.select_btn.setText(self._t("button.change_region"))
            self.record_btn.setEnabled(True)

            if self.border_window:
                self.border_window.close()
                self.border_window = None
            self.border_window = BorderWindow(self.recording_rect)
            self.border_window.show()
            self._debug_selection_state("selection border created and shown")
        except Exception as exc:
            debug_print(
                "[selection] exception in completion callback:",
                repr(exc),
                traceback.format_exc(),
            )
            self._set_status(
                "error",
                "status.selection_error.title",
                "status.selection_error.detail",
                error=str(exc),
            )
        finally:
            self._finish_region_selection(selector)

    def on_region_selection_canceled(self):
        selector = self.selector_overlay
        self._debug_selection_state("selection canceled")
        try:
            if self.recording_rect is None:
                self._set_status(
                    "select",
                    "status.select.title",
                    "status.cancelled.empty",
                )
            else:
                width, height = self.recording_rect[2:]
                self._set_status(
                    "ready",
                    "status.ready.title",
                    "status.cancelled.saved",
                    width=width,
                    height=height,
                )
            self.record_btn.setEnabled(self.recording_rect is not None)
        finally:
            self._finish_region_selection(selector)

    def on_region_selector_closed(self, *args):
        selector = self.sender()
        debug_print("[selection] selector destroyed:", repr(selector))
        if self.selector_overlay is selector:
            self.selector_overlay = None
        self.select_btn.setEnabled(True)
        self._restore_main_window_after_selection()

    def _finish_region_selection(self, selector):
        try:
            self._dispose_selector(selector)
        finally:
            self._restore_main_window_after_selection()
            self._debug_selection_state("selection completion finished")

    def _dispose_selector(self, selector):
        if selector is None:
            return

        if self.selector_overlay is selector:
            self.selector_overlay = None

        for signal, slot in (
            (selector.selection_made, self.on_region_selected),
            (selector.selection_canceled, self.on_region_selection_canceled),
            (selector.destroyed, self.on_region_selector_closed),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError) as exc:
                debug_print("[selection] signal disconnect skipped:", repr(exc))

        try:
            if not selector._closing:
                selector.close()
            selector.deleteLater()
        except RuntimeError as exc:
            debug_print("[selection] selector disposal failed:", repr(exc))

        self.select_btn.setEnabled(not self.recording)
        debug_print("[selection] selector closed, deleted, and disconnected")

    def _restore_main_window_after_selection(self):
        debug_print(
            "[selection] restoring main window; before:",
            "visible=", self.isVisible(),
            "minimized=", self.isMinimized(),
        )
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        debug_print(
            "[selection] restoring main window; after:",
            "visible=", self.isVisible(),
            "minimized=", self.isMinimized(),
        )

    def _debug_selection_state(self, event):
        stopping = bool(
            self.recorder_thread
            and self.recorder_thread.isRunning()
            and not self.recorder_thread.is_recording
        )
        debug_print(
            f"[selection] {event};",
            "main_visible=", self.isVisible(),
            "main_minimized=", self.isMinimized(),
            "recording=", self.recording,
            "stopping=", stopping,
            "selector=", repr(self.selector_overlay),
            "border=", repr(self.border_window),
        )

    def start_recording(self):
        # Защита от повторного старта
        if self.recorder_thread and self.recorder_thread.is_recording:
            return

        if not self.recording_rect:
            self._set_status(
                "select",
                "status.no_region.title",
                "status.no_region.detail",
            )
            return

        self.recording = True
        self.paused = False
        self.record_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.select_btn.setEnabled(False)
        self.audio_combo.setEnabled(False)
        self.recording_mode_combo.setEnabled(False)
        self._set_recording_actions(True)

        # Создать папку для сохранения.  Новое название папки — 'Мои записи'
        recordings_dir = recordings_directory()
        recordings_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        temp_video_filename = f"recording_{timestamp}_video.mkv"
        final_video_filename = f"recording_{timestamp}.mp4"
        self.temp_output_path = recordings_dir / temp_video_filename
        self.final_output_path = recordings_dir / final_video_filename
        output_file = self.temp_output_path

        profile_key = self.recording_mode_combo.currentData() or "tracker"
        video_profile = get_video_profile(profile_key)
        fps = video_profile.fps

        debug_print("\n" + "=" * 60)
        debug_print("НАЧАЛО НОВОЙ ЗАПИСИ")
        debug_print("=" * 60)

        audio_mode = self.audio_combo.currentData()
        audio_prefix = recordings_dir / f"recording_{timestamp}_audio"
        self.audio_session = AudioSession(
            audio_mode,
            audio_prefix,
            logger=debug_print,
            diagnostics=DEBUG,
        )
        try:
            self.audio_session.start()
        except AudioCaptureError as exc:
            debug_print(f"Audio start failed: {exc}")
            self.audio_session = None
            self.recording = False
            self.record_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            self.select_btn.setEnabled(True)
            self.audio_combo.setEnabled(True)
            self.recording_mode_combo.setEnabled(True)
            self._set_recording_actions(False)
            self._set_status(
                "error",
                "status.start_error.title",
                "status.start_error.detail",
                error=str(exc),
            )
            return

        self.recorder_thread = RecorderThread(
            self.recording_rect,
            str(output_file),
            profile_key,
        )
        self.recorder_thread.completed.connect(self.on_recording_finished)
        self.recorder_thread.error.connect(self.on_recording_error)
        self.recorder_thread.start()

        # Показать рамку вокруг области записи
        if self.border_window:
            self.border_window.close()
        self.border_window = BorderWindow(self.recording_rect)
        self.border_window.show()

        # Свернуть главное окно при старте записи
        self.showMinimized()
        self._debug_selection_state("main window minimized for recording")

        self._set_status(
            "recording",
            "status.recording.title",
            "status.recording.detail",
            width=self.recording_rect[2],
            height=self.recording_rect[3],
            fps=fps,
        )

    def toggle_pause(self):
        if not self.recorder_thread:
            return

        self.paused = not self.paused
        self.recorder_thread.is_paused = self.paused
        if self.audio_session is not None:
            self.audio_session.set_paused(self.paused)
        if self.paused:
            self.pause_btn.setText(self._t("button.resume"))
            self._set_status(
                "paused",
                "status.paused.title",
                "status.paused.detail",
            )
            debug_print("⏸️ Запись поставлена на паузу")
        else:
            self.pause_btn.setText(self._t("button.pause"))
            self._set_status(
                "recording",
                "status.recording.title",
                "status.resumed.detail",
            )
            debug_print("▶️ Запись продолжается")

    def stop_recording(self):
        if self.recorder_thread and self.recorder_thread.is_recording:
            self.recorder_thread.is_recording = False
            self._set_status(
                "saving",
                "status.saving.title",
                "status.saving.wait",
            )
            self.pause_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)
            debug_print("⏹️ Остановка записи...")

        if self.audio_session is not None:
            self.audio_session.request_stop()

        # Спрятать рамку сразу при остановке
        if self.border_window:
            self.border_window.close()
            self.border_window = None

    def on_recording_finished(self):
        debug_print("=" * 60)
        debug_print("ЗАПИСЬ ЗАВЕРШЕНА")
        debug_print("=" * 60 + "\n")

        self.recording = False
        self.record_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.select_btn.setEnabled(True)
        self.audio_combo.setEnabled(True)
        self.recording_mode_combo.setEnabled(True)
        self.pause_btn.setText(self._t("button.pause"))
        self._set_recording_actions(False)
        self._set_status(
            "saving",
            "status.saving.title",
            "status.saving.finalize",
        )
        self._debug_selection_state("recording finished")

        if self.border_window:
            self.border_window.close()
            self.border_window = None

        audio_tracks = []
        audio_error = None
        if self.audio_session is not None:
            try:
                audio_tracks = self.audio_session.stop()
            except AudioCaptureError as exc:
                audio_error = exc
                debug_print(f"Audio stop failed: {exc}")

        if not self.recorder_thread or not self.recorder_thread.output_path:
            self._set_status(
                "error",
                "status.save_error.title",
                "status.save_error.no_path",
            )
            self.audio_session = None
            return

        raw_path = Path(self.recorder_thread.output_path)
        final_path = Path(self.final_output_path)
        if not raw_path.exists():
            self._set_status(
                "error",
                "status.save_error.title",
                "status.save_error.missing_file",
                filename=raw_path.name,
            )
            debug_print(f"Raw video file not found: {raw_path}")
            self.audio_session = None
            return

        try:
            output_duration = (
                self.recorder_thread.frame_count
                / self.recorder_thread.effective_fps
            )
            mux_recording(
                raw_path,
                final_path,
                audio_tracks if audio_error is None else [],
                output_duration=output_duration,
                logger=debug_print,
                diagnostics=DEBUG,
            )
            raw_path.unlink()
            if self.audio_session is not None:
                self.audio_session.cleanup()

            video_duration = self.recorder_thread.active_recording_seconds
            audio_duration = max(
                (track.duration for track in audio_tracks),
                default=0.0,
            )
            if audio_tracks:
                debug_print(
                    f"Audio/video duration: audio={audio_duration:.3f}s "
                    f"video={video_duration:.3f}s "
                    f"delta={abs(audio_duration - video_duration):.3f}s"
                )

            size_mb = final_path.stat().st_size / (1024 * 1024)
            if audio_error is not None:
                self._set_status(
                    "saved",
                    "status.saved.title",
                    "status.saved.no_audio",
                    filename=final_path.name,
                    size=size_mb,
                )
            else:
                self._set_status(
                    "saved",
                    "status.saved.title",
                    "status.saved.detail",
                    filename=final_path.name,
                    size=size_mb,
                )
        except (MediaMuxError, OSError) as exc:
            debug_print(f"Recording mux failed: {exc}")
            self._set_status(
                "error",
                "status.save_error.title",
                "status.save_error.preserved",
                error=str(exc),
            )
        finally:
            self.audio_session = None

    def on_recording_error(self, error_msg):
        debug_print(f"❌ {error_msg}\n")
        self.recording = False
        self.record_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.select_btn.setEnabled(True)
        self.audio_combo.setEnabled(True)
        self.recording_mode_combo.setEnabled(True)
        self._set_recording_actions(False)
        technical_error = error_msg.removeprefix(
            "Ошибка при записи видео: "
        )
        if technical_error == "Не было захвачено ни одного кадра":
            detail_key = "status.recording_error.no_frames"
            detail_values = {}
        else:
            detail_key = "status.recording_error.detail"
            detail_values = {"error": technical_error}
        self._set_status(
            "error",
            "status.recording_error.title",
            detail_key,
            **detail_values,
        )
        self.recorder_thread = None

        if self.audio_session is not None:
            try:
                self.audio_session.stop()
            except AudioCaptureError as exc:
                debug_print(f"Audio stop after video error failed: {exc}")
            debug_print("Temporary audio files preserved after video error")
            self.audio_session = None

        if self.border_window:
            self.border_window.close()
            self.border_window = None

    def show_help(self):
        self.help_window = HelpWindow(self._t("help.title"), self)
        self.help_window.exec_()

    def open_recordings_folder(self):
        directory = recordings_directory()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(directory))
        except OSError as exc:
            self._set_status(
                "error",
                "status.folder_error.title",
                "status.folder_error.detail",
                error=str(exc),
            )


if __name__ == "__main__":
    enable_dpi_awareness()
    set_windows_app_user_model_id()
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(str(bundled_resource_path("screen-recorder-icon.ico"))))
    window = ScreenRecorder()
    window.show()
    sys.exit(app.exec_())
