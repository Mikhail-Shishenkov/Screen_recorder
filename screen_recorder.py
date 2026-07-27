import time
from datetime import datetime
from pathlib import Path
import sys
import os
import pyautogui
import ctypes
import shutil
import subprocess
import traceback


from PIL import ImageGrab
import cv2
import numpy as np
from PyQt5.QtGui import QMovie
from PyQt5.QtWidgets import QDialog
from region_geometry import (
    normalize_selection_rect,
    rect_to_capture_bbox,
    rect_to_capture_region,
)


from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QPushButton, QLabel, QComboBox, QSpinBox
)
from PyQt5.QtCore import pyqtSignal, QThread, Qt, QTimer, QRect

import platform
from PyQt5.QtGui import QPainter, QPen, QColor

# === ВКЛ/ВЫКЛ логов в терминал ===
DEBUG = False


def debug_print(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


def enable_dpi_awareness():
    """Make the process per-monitor DPI aware before QApplication exists."""
    if not sys.platform.startswith("win"):
        return

    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
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
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, bbox, output_path, fps=30):
        super().__init__()
        self.bbox = bbox  # (x, y, width, height)
        self.output_path = output_path
        self.fps = fps              # желаемый FPS (ограничение сверху)
        self.effective_fps = fps    # фактический FPS, с которым будем писать
        self.is_recording = True
        self.is_paused = False

        # frames оставляем для совместимости, но не используем
        self.frames = []
        self.frame_count = 0
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

    def _measure_capture_fps(self, capture_bbox, capture_region, duration=1.0):
        """
        Быстренько меряем реальный FPS захвата экрана,
        чтобы не ускорять/замедлять итоговое видео.
        """
        debug_print("⏱️ Калибровка FPS захвата...")
        start = time.time()
        frames = 0

        while time.time() - start < duration and self.is_recording:
            try:
                self._capture_frame_raw(capture_bbox, capture_region)
                frames += 1
            except Exception as e:
                debug_print(f"⚠️ Ошибка при калибровке FPS: {e}")
                break

        elapsed = time.time() - start
        if frames > 0 and elapsed > 0:
            capture_fps = frames / elapsed
            debug_print(f"   Кадров за калибровку: {frames}, время: {elapsed:.2f}с")
            debug_print(f"   Оценка FPS захвата: {capture_fps:.1f}")
            return capture_fps

        debug_print("⚠️ Не удалось померить FPS, используем заданный FPS")
        return float(self.fps)

    def run(self):
        """
        Захватывает область экрана и пишет кадры в VideoWriter.
        Перед началом записи калибруем реальный FPS захвата и
        используем min(реальный_fps, выбранный_fps), чтобы избежать
        ускорения/замедления при воспроизведении.
        """
        writer = None
        try:
            debug_print(f"Начало записи в {self.output_path}")
            debug_print(f"Область: {self.bbox}")

            x, y, w, h = self.bbox
            capture_bbox = rect_to_capture_bbox(self.bbox)
            capture_region = rect_to_capture_region(self.bbox)

            # --- КАЛИБРОВКА FPS ---
            capture_fps = self._measure_capture_fps(capture_bbox, capture_region, duration=1.0)
            # Фактический FPS: не выше желаемого и не ниже 5, чтобы плееры не сходили с ума
            self.effective_fps = max(5.0, min(capture_fps, float(self.fps)))
            frame_period = 1.0 / self.effective_fps

            debug_print(f"🎯 Желаемый FPS: {self.fps}")
            debug_print(f"🎯 Реальный FPS захвата: {capture_fps:.1f}")
            debug_print(f"🎯 Итоговый FPS записи: {self.effective_fps:.1f}")

            start_time = time.time()

            while self.is_recording:
                if not self.is_paused:
                    frame_start_time = time.time()
                    try:
                        # Захват скриншота
                        if self.use_pyautogui_capture:
                            screenshot = pyautogui.screenshot(region=capture_region)
                            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)
                        else:
                            screenshot = self._grab_pil_frame(capture_bbox)
                            frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_BGR2RGB)
                            # NOTE: предыдущая строка была RGB2BGR, оставь как было, если цвета уйдут.
                            # Я просто показываю место, где можно поправить, если нужно.

                        # ===== Рисуем курсор и клики =====
                        mouse_x, mouse_y = pyautogui.position()
                        rel_x = mouse_x - x
                        rel_y = mouse_y - y
                        if 0 <= rel_x < w and 0 <= rel_y < h:
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

                        # Создаём VideoWriter на первом кадре с ИТОГОВЫМ FPS
                        if writer is None:
                            output_path_str = str(self.output_path)
                            # Для AVI используем кодек XVID; для MP4 – mp4v.
                            if output_path_str.lower().endswith('.avi'):
                                fourcc = cv2.VideoWriter_fourcc(*'XVID')
                            else:
                                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                            h_frame, w_frame = frame.shape[:2]
                            writer = cv2.VideoWriter(
                                output_path_str,
                                fourcc,
                                self.effective_fps,
                                (w_frame, h_frame)
                            )
                            if not writer.isOpened():
                                raise Exception("Не удалось открыть VideoWriter - проверьте кодеки")

                        # Пишем кадр
                        writer.write(frame)
                        self.frame_count += 1
                        if self.frame_count % 30 == 0:
                            debug_print(f"✓ Захвачено кадров: {self.frame_count}")

                        # Держим реальное время в такт FPS
                        elapsed = time.time() - frame_start_time
                        sleep_time = frame_period - elapsed
                        if sleep_time > 0:
                            time.sleep(sleep_time)

                    except Exception as e:
                        debug_print(f"⚠️ Ошибка захвата: {e}")
                        time.sleep(0.05)
                        continue
                else:
                    time.sleep(0.01)

            # --- Завершение ---
            total_elapsed = time.time() - start_time
            debug_print(f"\n📊 Захват завершен:")
            debug_print(f"   Кадров: {self.frame_count}")
            debug_print(f"   Время: {total_elapsed:.1f}с")
            actual_fps = self.frame_count / total_elapsed if total_elapsed > 0 else self.effective_fps
            debug_print(f"   Фактический FPS по счёту: {actual_fps:.1f}\n")

            if writer is not None:
                writer.release()

            if self.frame_count == 0:
                raise Exception("Не было захвачено ни одного кадра")

            output_path_str = str(self.output_path)
            if Path(output_path_str).exists():
                file_size = Path(output_path_str).stat().st_size / (1024 * 1024)
                debug_print(
                    f"\n✅ Видео успешно сохранено! Размер: {file_size:.1f} MB; кадры: {self.frame_count}; "
                    f"FPS записи: {self.effective_fps:.1f}\n"
                )
            else:
                debug_print("\n⚠️ Видео файл не найден после завершения записи!\n")

        except Exception as e:
            error_msg = f"Ошибка при записи видео: {str(e)}"
            debug_print(f"❌ {error_msg}\n")
            self.error.emit(error_msg)
        finally:
            self.frames = []
            self.is_recording = False
            self.finished.emit()


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.virtual_geometry = get_virtual_screen_geometry()
        self.start_point = None
        self.current_point = None
        self.message = "Drag to select an area. Esc cancels."
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
        self.message = "Release to confirm. Esc cancels."
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
            self.message = "Area must be at least 2x2 px after even rounding."
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🕷️ Help from Spider-Man")
        self.setFixedSize(400, 400)

        layout = QVBoxLayout(self)

        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.label)

        base_path = getattr(sys, "_MEIPASS", os.path.abspath("."))
        gif_path = os.path.join(base_path, "spider-man-dance.gif")

        self.movie = QMovie(gif_path)
        self.label.setMovie(self.movie)
        self.movie.start()

class ScreenRecorder(QMainWindow):
    def __init__(self):
        super().__init__()
        # Set the application title and a fixed aspect ratio.  Baroque designs
        # typically have generous proportions; the user requested a 4:3 ratio
        # roughly 740×400.  We'll set this directly and disallow resizing to
        # preserve the layout.
        self.setWindowTitle("Screen Recorder Pro")
        # Increase the default window size so that longer instructional text fits
        # comfortably.  This also makes the controls easier to read.
        self.setFixedSize(800, 500)

        self.recording = False
        self.paused = False
        self.recorder_thread = None
        self.recording_rect = None
        self.border_window = None
        self.selector_overlay = None
        self.keyboard = None  # модуль для глобальных хоткеев
        # Previously, an audio_thread attribute was used to record audio.  Since
        # audio recording has been removed, we no longer initialize it.

        # UI
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.status_label = QLabel("Нажмите 'Выбрать область' для начала")
        # Apply baroque-inspired styling: soft colours, serif font and subtle backgrounds
        self.status_label.setStyleSheet(
            "font-size: 18px; font-family: 'Palatino Linotype', 'Georgia', serif; "
            "color: #4a5f8f; padding: 10px; font-weight: bold;"
        )
        layout.addWidget(self.status_label)

        # Кнопки
        buttons_layout = QHBoxLayout()
        self.select_btn = QPushButton("📐 Выбрать область")
        self.select_btn.setMinimumHeight(40)
        self.select_btn.clicked.connect(self.select_region)

        self.record_btn = QPushButton("🔴 Записать")
        self.record_btn.setMinimumHeight(40)
        self.record_btn.clicked.connect(self.start_recording)
        self.record_btn.setEnabled(False)

        self.pause_btn = QPushButton("⏸️ Пауза")
        self.pause_btn.setMinimumHeight(40)
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.pause_btn.setEnabled(False)

        self.stop_btn = QPushButton("⏹️ Стоп")
        self.stop_btn.setMinimumHeight(40)
        self.stop_btn.clicked.connect(self.stop_recording)
        self.stop_btn.setEnabled(False)

        self.help_btn = QPushButton("🕷️ Help")
        self.help_btn.setMinimumHeight(40)
        self.help_btn.clicked.connect(self.show_help)

        buttons_layout.addWidget(self.select_btn)
        buttons_layout.addWidget(self.record_btn)
        buttons_layout.addWidget(self.pause_btn)
        buttons_layout.addWidget(self.stop_btn)
        buttons_layout.addWidget(self.help_btn)
        layout.addLayout(buttons_layout)

        # Параметры
        params_layout = QHBoxLayout()
        params_layout.setSpacing(20)

        # FPS selector
        fps_label = QLabel("FPS:")
        fps_label.setStyleSheet("font-family: 'Palatino Linotype', serif; color: #4a5f8f;")
        params_layout.addWidget(fps_label)

        self.fps_spin = QSpinBox()
        self.fps_spin.setValue(30)
        self.fps_spin.setRange(10, 60)
        self.fps_spin.setStyleSheet(
            "background: #f2e8d8; border: 1px solid #4a5f8f; border-radius: 3px; padding: 2px; "
            "font-family: 'Palatino Linotype', serif; color: #4a5f8f;"
        )
        params_layout.addWidget(self.fps_spin)

        # Quality drop-down
        quality_label = QLabel("Качество:")
        quality_label.setStyleSheet("font-family: 'Palatino Linotype', serif; color: #4a5f8f;")
        params_layout.addWidget(quality_label)

        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["Высокое (30 FPS)", "Среднее (24 FPS)", "Экономно (15 FPS)"])
        self.quality_combo.setStyleSheet(
            "QComboBox { background: #f2e8d8; border: 1px solid #4a5f8f; "
            "border-radius: 3px; padding: 2px; font-family: 'Palatino Linotype', serif; color: #4a5f8f; }"
            "QComboBox::drop-down { border: 0px; }"
        )
        params_layout.addWidget(self.quality_combo)

        # Note: audio recording support has been removed.  If needed, use a
        # separate tool to capture system audio.

        params_layout.addStretch()
        layout.addLayout(params_layout)

        # Инструкция
        info_text = (
            "ℹ️ Инструкция:\n"
            "1. Нажмите 'Выбрать область' и выделите прямоугольник мышью.\n"
            "2. Нажмите 'Записать' для начала записи (окно будет свернуто).\n"
            "3. Горячие клавиши (глобальные):\n"
            "   • Ctrl+1 — старт записи\n"
            "   • Ctrl+2 — пауза/продолжить\n"
            "   • Ctrl+3 — стоп записи\n"
            "4. Во время записи вокруг выбранной области будет красная рамка.\n"
            "5. Видео сохраняется в папку 'Мои записи' в формате MP4.\n"
            "   Для воспроизведения в браузере требуется наличие ffmpeg: если он\n"
            "   установлен, файл будет автоматически перекодирован в MP4 (H.264).\n"
            "   Без ffmpeg видео сохраняется в сыром формате mp4v, который может\n"
            "   не воспроизводиться в браузерах. Установите ffmpeg для лучшей\n"
            "   совместимости."
        )
        info = QLabel(info_text)
        info.setWordWrap(True)
        info.setStyleSheet(
            "color: #5a6f8f; font-size: 14px; margin: 15px; padding: 12px; "
            "background: #f2e8d8; border-radius: 5px; border-left: 4px solid #4a5f8f; "
            "font-family: 'Palatino Linotype', serif;"
        )
        layout.addWidget(info)
        layout.addStretch()

        # Apply a global stylesheet for a baroque-inspired look.  Soft gradients,
        # ornate colours and serif fonts evoke a classic feel.  Buttons
        # highlight on hover and disabled states are dimmed gracefully.
        self.setStyleSheet(
            "QMainWindow {"
            "    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,"
            "        stop:0 #e8dcc8, stop:0.5 #f2e8d8, stop:1 #e8dcc8);"
            "    font-family: 'Palatino Linotype', 'Book Antiqua', 'Georgia', serif;"
            "    color: #4a5f8f;"
            "    font-size: 16px;"
            "}"
            "QPushButton {"
            "    background-color: #8b6f47;"
            "    color: white;"
            "    border: 1px solid #4a5f8f;"
            "    border-radius: 4px;"
            "    padding: 8px 16px;"
            "    font-family: 'Palatino Linotype', serif;"
            "    font-size: 16px;"
            "    font-weight: bold;"
            "}"
            "QPushButton:hover {"
            "    background-color: #a68a6a;"
            "}"
            "QPushButton:disabled {"
            "    background-color: #d0c6b1;"
            "    color: #9b8c76;"
            "    border-color: #a69b87;"
            "}"
            "QLabel {"
            "    font-family: 'Palatino Linotype', serif;"
            "    font-size: 16px;"
            "}"
        )

        # Пытаемся подключить глобальные горячие клавиши
        self.init_hotkeys()

    # === ГОРЯЧИЕ КЛАВИШИ (глобальные через модуль keyboard) ===
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
            self.status_label.setText(
                "Горячие клавиши недоступны: установите пакет 'keyboard' (pip install keyboard)"
            )

    def select_region(self):
        self._debug_selection_state("select button handler entered")
        self.status_label.setText("📌 Выбирайте область... (ESC для отмены)")
        if self.selector_overlay and self.selector_overlay.isVisible():
            self._debug_selection_state("existing selector is still visible")
            return

        if self.selector_overlay:
            self._dispose_selector(self.selector_overlay)

        self.select_btn.setEnabled(False)
        self.selector_overlay = RegionSelectorOverlay()
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
            self.status_label.setText(f"✅ Выбрана область: {w}×{h} пикселей. Готово к записи.")
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
            self.status_label.setText(f"Selection failed: {exc}")
        finally:
            self._finish_region_selection(selector)

    def on_region_selection_canceled(self):
        selector = self.selector_overlay
        self._debug_selection_state("selection canceled")
        try:
            self.status_label.setText("Selection canceled.")
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
            self.status_label.setText("❌ Сначала выберите область!")
            return

        self.recording = True
        self.paused = False
        self.record_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self.select_btn.setEnabled(False)

        # Создать папку для сохранения.  Новое название папки — 'Мои записи'
        recordings_dir = Path("Мои записи")
        recordings_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Имя временного файла для записи.  Используем расширение .mp4 для
        # промежуточного файла; OpenCV запишет поток с кодеком mp4v (MPEG-4 Part 2).
        # После завершения записи при наличии ffmpeg файл будет перекодирован
        # в H.264, а исходный файл будет переименован.
        temp_video_filename = f"recording_{timestamp}_raw.mp4"
        final_video_filename = f"recording_{timestamp}.mp4"
        self.temp_output_path = recordings_dir / temp_video_filename
        self.final_output_path = recordings_dir / final_video_filename
        output_file = self.temp_output_path

        # Получить FPS из комбо
        fps_map = {0: 30, 1: 24, 2: 15}
        fps = fps_map.get(self.quality_combo.currentIndex(), 30)
        self.fps_spin.setValue(fps)

        debug_print("\n" + "=" * 60)
        debug_print("НАЧАЛО НОВОЙ ЗАПИСИ")
        debug_print("=" * 60)

        self.recorder_thread = RecorderThread(
            self.recording_rect,
            str(output_file),
            fps
        )
        self.recorder_thread.finished.connect(self.on_recording_finished)
        self.recorder_thread.error.connect(self.on_recording_error)
        self.recorder_thread.start()

        # Audio recording support has been removed, so we no longer start
        # a separate audio thread.  If you need to record sound, please use
        # an external tool in parallel with this screen recorder.

        # Показать рамку вокруг области записи
        if self.border_window:
            self.border_window.close()
        self.border_window = BorderWindow(self.recording_rect)
        self.border_window.show()

        # Свернуть главное окно при старте записи
        self.showMinimized()
        self._debug_selection_state("main window minimized for recording")

        self.status_label.setText(
            f"⏺️ ЗАПИСЬ в процессе... {self.recording_rect[2]}×{self.recording_rect[3]}px @ {fps} FPS"
        )

    def toggle_pause(self):
        if not self.recorder_thread:
            return

        self.paused = not self.paused
        self.recorder_thread.is_paused = self.paused
        if self.paused:
            self.pause_btn.setText("⏯️ Продолжить")
            # Update status text with new hotkey (Ctrl+2) for resume
            self.status_label.setText("⏸️ ПАУЗА - нажмите 'Продолжить' или Ctrl+2")
            debug_print("⏸️ Запись поставлена на паузу")
        else:
            self.pause_btn.setText("⏸️ Пауза")
            self.status_label.setText("⏺️ ЗАПИСЬ продолжается...")
            debug_print("▶️ Запись продолжается")

    def stop_recording(self):
        if self.recorder_thread and self.recorder_thread.is_recording:
            self.recorder_thread.is_recording = False
            self.status_label.setText("⏹️ Завершение записи... (пожалуйста, ждите)")
            self.stop_btn.setEnabled(False)
            debug_print("⏹️ Остановка записи...")

        # Audio recording has been removed; no audio thread to stop

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
        self.stop_btn.setEnabled(True)
        self.select_btn.setEnabled(True)
        self.pause_btn.setText("⏸️ Пауза")
        self._debug_selection_state("recording finished")

        # Убедимся, что рамка закрыта
        if self.border_window:
            self.border_window.close()
            self.border_window = None

        # No audio thread to wait for; audio recording has been removed

        if self.recorder_thread and self.recorder_thread.output_path:
            raw_path = Path(self.recorder_thread.output_path)
            if raw_path.exists():
                # Попробуем перекодировать файл в H.264, если установлен ffmpeg
                final_msg = ""
                if hasattr(self, 'final_output_path') and hasattr(self, 'temp_output_path'):
                    try:
                        # Проверяем наличие ffmpeg
                        ffmpeg_path = shutil.which('ffmpeg')
                        if ffmpeg_path:
                            # Выполняем конвертацию: mp4v -> h264
                            final_path = Path(self.final_output_path)
                            cmd = [
                                ffmpeg_path, '-y',
                                '-i', str(raw_path),
                                '-c:v', 'libx264',
                                '-pix_fmt', 'yuv420p',
                                str(final_path)
                            ]
                            debug_print(f"Запуск ffmpeg: {' '.join(cmd)}")
                            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            if result.returncode == 0 and final_path.exists():
                                # Удаляем исходный временный файл
                                try:
                                    raw_path.unlink()
                                except Exception:
                                    pass
                                size_mb = final_path.stat().st_size / (1024 * 1024)
                                final_msg = (
                                    f"✅ ГОТОВО! Видео сохранено ({size_mb:.1f} MB) в формате H.264."
                                )
                                self.status_label.setText(final_msg)
                                debug_print(f"✅ Файл успешно перекодирован в H.264: {final_path}\n")
                            else:
                                # ffmpeg нашелся, но конвертация не удалась
                                final_msg = ("⚠️ ffmpeg не смог конвертировать видео. "
                                             "Будет использован исходный файл.")
                                self.status_label.setText(final_msg)
                                size_mb = raw_path.stat().st_size / (1024 * 1024)
                                debug_print(f"⚠️ ffmpeg завершился с кодом {result.returncode}. "
                                            f"Исходный файл {raw_path} ({size_mb:.1f} MB) будет оставлен.\n")
                        else:
                            # ffmpeg не найден – просто переименуем файл
                            if hasattr(self, 'final_output_path'):
                                try:
                                    raw_path.rename(self.final_output_path)
                                    final_path = Path(self.final_output_path)
                                    size_mb = final_path.stat().st_size / (1024 * 1024)
                                    final_msg = (
                                        f"⚠️ ffmpeg не найден, поэтому видео сохранено как RAW MP4 ({size_mb:.1f} MB)."
                                    )
                                    self.status_label.setText(final_msg)
                                    debug_print(f"⚠️ ffmpeg не найден. Видео сохранено без перекодирования: {final_path}\n")
                                except Exception:
                                    size_mb = raw_path.stat().st_size / (1024 * 1024)
                                    self.status_label.setText(
                                        f"❌ Ошибка переименования. Видео сохранено как RAW MP4 ({size_mb:.1f} MB)."
                                    )
                                    debug_print(
                                        f"❌ Ошибка переименования файла {raw_path}. Видео сохранено без изменения.\n"
                                    )
                    except Exception as e:
                        # Любая ошибка при конвертации
                        size_mb = raw_path.stat().st_size / (1024 * 1024)
                        self.status_label.setText(
                            f"⚠️ Ошибка конвертации: {str(e)}. Видео сохранено как RAW MP4 ({size_mb:.1f} MB)."
                        )
                        debug_print(f"⚠️ Ошибка при вызове ffmpeg: {e}\n")
                else:
                    # Нет информации о файлах — просто показываем, что файл сохранен
                    size_mb = raw_path.stat().st_size / (1024 * 1024)
                    self.status_label.setText(
                        f"✅ ГОТОВО! Видео сохранено ({size_mb:.1f} MB)"
                    )
                    debug_print(f"✅ Файл успешно сохранен: {raw_path}\n")
            else:
                self.status_label.setText("❌ Файл не найден!")
                debug_print(f"❌ Ошибка: файл не найден {raw_path}\n")
        else:
            self.status_label.setText("❌ Ошибка при сохранении!")
            debug_print("❌ Ошибка: не удалось сохранить видео\n")

    def on_recording_error(self, error_msg):
        debug_print(f"❌ {error_msg}\n")
        self.status_label.setText(f"❌ {error_msg}")
        self.record_btn.setEnabled(True)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.select_btn.setEnabled(True)
        self.recorder_thread = None

        if self.border_window:
            self.border_window.close()
            self.border_window = None

    def show_help(self):
        self.help_window = HelpWindow()
        self.help_window.exec_()


if __name__ == "__main__":
    enable_dpi_awareness()
    app = QApplication(sys.argv)
    window = ScreenRecorder()
    window.show()
    sys.exit(app.exec_())
