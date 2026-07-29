import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

from ffmpeg_video_writer import VIDEO_PROFILES
from localization import (
    DEFAULT_LANGUAGE,
    LANGUAGE_OPTIONS,
    TRANSLATIONS,
    normalize_language,
    translate,
)
from screen_recorder import (
    AUDIO_MODE_TRANSLATION_KEYS,
    HelpWindow,
    ScreenRecorder,
)


class _FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, slot):
        self._slots.append(slot)

    def emit(self, *args):
        for slot in tuple(self._slots):
            slot(*args)


class _FakeRecorderThread:
    def __init__(self, bbox, output_path, profile_key):
        self.bbox = bbox
        self.output_path = output_path
        self.profile_key = profile_key
        self.is_recording = True
        self.is_paused = False
        self.completed = _FakeSignal()
        self.error = _FakeSignal()
        self.frame_count = 15
        self.effective_fps = 15
        self.active_recording_seconds = 1.0
        self._running = False

    def start(self):
        Path(self.output_path).write_bytes(b"video")
        self._running = True

    def isRunning(self):
        return self._running

    def complete(self):
        self.is_recording = False
        self._running = False
        self.completed.emit()


class _FakeAudioSession:
    def __init__(self, mode=None, *args, **kwargs):
        self.mode = mode
        self.cleaned = False

    def start(self):
        pass

    def request_stop(self):
        pass

    def set_paused(self, paused):
        pass

    def stop(self):
        return []

    def cleanup(self):
        self.cleaned = True


class TranslationTableTests(unittest.TestCase):
    def test_all_supported_languages_are_present(self):
        self.assertEqual(
            tuple(TRANSLATIONS),
            tuple(language for language, _ in LANGUAGE_OPTIONS),
        )
        self.assertEqual(DEFAULT_LANGUAGE, "ru")

    def test_translations_are_complete_and_non_empty(self):
        russian_keys = set(TRANSLATIONS["ru"])
        for language in ("en", "it"):
            with self.subTest(language=language):
                self.assertEqual(set(TRANSLATIONS[language]), russian_keys)
        for language, translations in TRANSLATIONS.items():
            for key, value in translations.items():
                with self.subTest(language=language, key=key):
                    self.assertIsInstance(value, str)
                    self.assertTrue(value.strip())

    def test_unknown_language_and_key_use_safe_fallbacks(self):
        self.assertEqual(normalize_language("de"), "ru")
        self.assertEqual(
            translate("de", "button.start"),
            TRANSLATIONS["ru"]["button.start"],
        )
        self.assertEqual(
            translate("en", "missing.translation.key"),
            "missing.translation.key",
        )


class LocalizationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "settings.ini"
        self.settings = QSettings(
            str(self.settings_path),
            QSettings.IniFormat,
        )
        self.hotkeys_patcher = patch.object(
            ScreenRecorder,
            "init_hotkeys",
            lambda recorder: None,
        )
        self.hotkeys_patcher.start()
        self.recorder = ScreenRecorder(settings=self.settings)
        self.recorder.show()
        self.app.processEvents()

    def tearDown(self):
        if self.recorder.border_window:
            self.recorder.border_window.close()
        if self.recorder.selector_overlay:
            self.recorder.selector_overlay.close()
        self.recorder.close()
        self.app.processEvents()
        self.hotkeys_patcher.stop()
        self.temp_dir.cleanup()

    def _combo_texts(self, combo):
        return [
            combo.itemText(index)
            for index in range(combo.count())
        ]

    def test_russian_is_default_and_all_visible_elements_switch_immediately(self):
        self.assertEqual(self.recorder.current_language, "ru")
        self.assertEqual(
            [
                self.recorder.language_combo.itemData(index)
                for index in range(self.recorder.language_combo.count())
            ],
            ["ru", "en", "it"],
        )
        self.assertEqual(
            self._combo_texts(self.recorder.language_combo),
            ["Русский", "English", "Italiano"],
        )

        expected = {
            "en": {
                "subtitle": "Record a selected area without extra setup",
                "mode": "Recording mode",
                "audio": "Audio",
                "select": "Select area",
                "record": "Start recording",
                "pause": "Pause",
                "stop": "Stop",
                "folder": "Open folder",
                "status": "Select a screen area",
            },
            "it": {
                "subtitle": (
                    "Registra un'area selezionata senza configurazioni extra"
                ),
                "mode": "Modalità di registrazione",
                "audio": "Audio",
                "select": "Seleziona area",
                "record": "Avvia registrazione",
                "pause": "Pausa",
                "stop": "Termina",
                "folder": "Apri cartella",
                "status": "Seleziona un'area dello schermo",
            },
            "ru": {
                "subtitle": "Запись выбранной области без лишних настроек",
                "mode": "Режим записи",
                "audio": "Звук",
                "select": "Выбрать область",
                "record": "Начать запись",
                "pause": "Пауза",
                "stop": "Завершить",
                "folder": "Открыть папку",
                "status": "Выберите область экрана",
            },
        }

        for language in ("en", "it", "ru"):
            with self.subTest(language=language):
                self.recorder.set_language(language)
                values = expected[language]
                self.assertEqual(
                    self.recorder.subtitle_label.text(),
                    values["subtitle"],
                )
                self.assertEqual(self.recorder.mode_label.text(), values["mode"])
                self.assertEqual(
                    self.recorder.sound_label.text(),
                    values["audio"],
                )
                self.assertEqual(
                    self.recorder.select_btn.text(),
                    values["select"],
                )
                self.assertEqual(
                    self.recorder.record_btn.text(),
                    values["record"],
                )
                self.assertEqual(
                    self.recorder.pause_btn.text(),
                    values["pause"],
                )
                self.assertEqual(
                    self.recorder.stop_btn.text(),
                    values["stop"],
                )
                self.assertEqual(
                    self.recorder.open_folder_btn.text(),
                    values["folder"],
                )
                self.assertEqual(
                    self.recorder.status_title.text(),
                    values["status"],
                )
                self.assertTrue(self.recorder.shortcuts_label.text())
                self.assertTrue(self.recorder.footer_label.text())
                self.assertTrue(self.recorder.help_btn.toolTip())

    def test_invalid_language_and_overlay_messages_fall_back_safely(self):
        self.recorder.set_language("de")
        self.assertEqual(self.recorder.current_language, "ru")
        self.assertEqual(
            self.recorder.settings.value("ui/language"),
            "ru",
        )

        self.recorder.set_language("en")
        self.recorder.select_region()
        try:
            self.assertEqual(
                self.recorder.selector_overlay.message,
                "Drag to select an area. Esc cancels.",
            )
        finally:
            self.recorder.selector_overlay.selection_canceled.emit()
            self.app.processEvents()

        help_window = HelpWindow(
            self.recorder._t("help.title"),
            self.recorder,
        )
        try:
            self.assertEqual(
                help_window.windowTitle(),
                "Help from Spider-Man",
            )
        finally:
            help_window.close()

    def test_selection_status_and_button_retranslate_with_parameters(self):
        with patch("screen_recorder.BorderWindow"):
            self.recorder.on_region_selected(10, 20, 640, 480)

        self.recorder.set_language("en")
        self.assertEqual(self.recorder.select_btn.text(), "Change area")
        self.assertEqual(self.recorder.status_title.text(), "Ready to record")
        self.assertEqual(
            self.recorder.status_detail.text(),
            "Selected area: 640 × 480 px.",
        )

        self.recorder.set_language("it")
        self.assertEqual(self.recorder.select_btn.text(), "Cambia area")
        self.assertEqual(
            self.recorder.status_detail.text(),
            "Area selezionata: 640 × 480 px.",
        )

    def test_language_is_saved_and_restored_by_a_new_window(self):
        self.recorder.set_language("it")
        self.recorder.close()
        self.app.processEvents()

        restored_settings = QSettings(
            str(self.settings_path),
            QSettings.IniFormat,
        )
        restored = ScreenRecorder(settings=restored_settings)
        try:
            self.assertEqual(restored.current_language, "it")
            self.assertEqual(
                restored.subtitle_label.text(),
                "Registra un'area selezionata senza configurazioni extra",
            )
        finally:
            restored.close()

    def test_mode_values_and_selection_survive_language_switches(self):
        profile_values = [
            self.recorder.recording_mode_combo.itemData(index)
            for index in range(self.recorder.recording_mode_combo.count())
        ]
        audio_values = [
            self.recorder.audio_combo.itemData(index)
            for index in range(self.recorder.audio_combo.count())
        ]
        self.assertEqual(
            profile_values,
            [profile.key for profile in VIDEO_PROFILES],
        )
        self.assertEqual(
            audio_values,
            list(AUDIO_MODE_TRANSLATION_KEYS),
        )

        self.recorder.recording_mode_combo.setCurrentIndex(1)
        self.recorder.audio_combo.setCurrentIndex(3)
        for language in ("en", "it", "ru"):
            self.recorder.set_language(language)
            self.assertEqual(
                self.recorder.recording_mode_combo.currentData(),
                "maximum",
            )
            self.assertEqual(
                self.recorder.audio_combo.currentData(),
                "system_microphone",
            )

        self.recorder.set_language("en")
        self.assertEqual(
            self._combo_texts(self.recorder.recording_mode_combo),
            [
                "For sharing - recommended",
                "Maximum quality",
                "Compact size",
            ],
        )
        self.assertEqual(
            self._combo_texts(self.recorder.audio_combo),
            [
                "No audio",
                "System audio",
                "Microphone",
                "System audio and microphone",
            ],
        )

    def test_italian_language_survives_three_recording_cycles(self):
        self.recorder.set_language("it")
        self.recorder.recording_rect = (0, 0, 64, 48)
        self.recorder.record_btn.setEnabled(True)
        audio_sessions = []

        def fake_mux(raw_video, final_video, *args, **kwargs):
            Path(final_video).write_bytes(b"mp4")

        def create_audio_session(*args, **kwargs):
            session = _FakeAudioSession(*args, **kwargs)
            audio_sessions.append(session)
            return session

        with patch(
            "screen_recorder.recordings_directory",
            return_value=Path(self.temp_dir.name),
        ), patch(
            "screen_recorder.RecorderThread",
            _FakeRecorderThread,
        ), patch(
            "screen_recorder.AudioSession",
            side_effect=create_audio_session,
        ), patch(
            "screen_recorder.mux_recording",
            side_effect=fake_mux,
        ):
            for profile_index in range(3):
                self.recorder.recording_mode_combo.setCurrentIndex(
                    profile_index
                )
                self.recorder.audio_combo.setCurrentIndex(profile_index + 1)
                self.recorder.start_recording()
                worker = self.recorder.recorder_thread
                self.assertEqual(
                    worker.profile_key,
                    self.recorder.recording_mode_combo.currentData(),
                )
                self.assertEqual(
                    audio_sessions[-1].mode,
                    self.recorder.audio_combo.currentData(),
                )
                self.recorder.stop_recording()
                worker.complete()
                self.app.processEvents()
                self.assertEqual(self.recorder.current_language, "it")
                self.assertEqual(
                    self.recorder.status_title.text(),
                    "Registrazione salvata",
                )


if __name__ == "__main__":
    unittest.main()
