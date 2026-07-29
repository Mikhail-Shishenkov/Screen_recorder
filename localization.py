DEFAULT_LANGUAGE = "ru"

LANGUAGE_OPTIONS = (
    ("ru", "Русский"),
    ("en", "English"),
    ("it", "Italiano"),
)

TRANSLATIONS = {
    "ru": {
        "app.title": "Screen Recorder Pro",
        "app.subtitle": "Запись выбранной области без лишних настроек",
        "language.tooltip": "Язык интерфейса",
        "help.tooltip": "Справка и Spider-Man",
        "help.title": "Помощь от Spider-Man",
        "field.recording_mode": "Режим записи",
        "field.audio": "Звук",
        "button.select_region": "Выбрать область",
        "button.change_region": "Изменить область",
        "button.start": "Начать запись",
        "button.pause": "Пауза",
        "button.resume": "Продолжить",
        "button.stop": "Завершить",
        "button.open_folder": "Открыть папку",
        "shortcuts.available": (
            "Горячие клавиши:  Ctrl+1 запись   •   "
            "Ctrl+2 пауза / продолжить   •   Ctrl+3 завершить"
        ),
        "shortcuts.unavailable": (
            "Горячие клавиши недоступны — используйте кнопки приложения."
        ),
        "footer.recordings": (
            "Записи сохраняются в папку «Мои записи» рядом с приложением."
        ),
        "quality.tracker": "Для отправки - рекомендуется",
        "quality.maximum": "Максимальное качество",
        "quality.compact": "Компактный размер",
        "audio.off": "Без звука",
        "audio.system": "Системный звук",
        "audio.microphone": "Микрофон",
        "audio.system_microphone": "Системный звук и микрофон",
        "overlay.select": "Выделите область перетаскиванием. Esc отменяет выбор.",
        "overlay.release": "Отпустите кнопку для подтверждения. Esc отменяет выбор.",
        "overlay.too_small": (
            "После округления область должна быть не меньше 2×2 px."
        ),
        "status.select.title": "Выберите область экрана",
        "status.select.initial": "Выделите область будущей записи.",
        "status.select.overlay": "Esc отменяет выбор области.",
        "status.ready.title": "Готово к записи",
        "status.ready.selected": "Выбрана область {width} × {height} px.",
        "status.selection_error.title": "Не удалось выбрать область",
        "status.selection_error.detail": "{error}",
        "status.cancelled.empty": (
            "Выбор отменён. Рамка покажет, какая часть экрана попадёт в видео."
        ),
        "status.cancelled.saved": (
            "Выбор отменён. Сохранена область {width} × {height} px."
        ),
        "status.no_region.title": "Сначала выберите область экрана",
        "status.no_region.detail": (
            "После выбора станет доступна кнопка «Начать запись»."
        ),
        "status.start_error.title": "Не удалось начать запись",
        "status.start_error.detail": "{error}",
        "status.recording.title": "Идёт запись",
        "status.recording.detail": (
            "Запись области {width} × {height} px, {fps} FPS."
        ),
        "status.paused.title": "Запись приостановлена",
        "status.paused.detail": (
            "Нажмите Ctrl+2 или «Продолжить», чтобы продолжить."
        ),
        "status.resumed.detail": "Запись продолжается.",
        "status.saving.title": "Сохранение видео",
        "status.saving.wait": "Пожалуйста, подождите.",
        "status.saving.finalize": "Подготавливаем итоговый MP4.",
        "status.save_error.title": "Ошибка сохранения",
        "status.save_error.no_path": "Не найден путь к временному видео.",
        "status.save_error.missing_file": (
            "Не найден временный файл: {filename}."
        ),
        "status.save_error.preserved": (
            "{error}. Временные файлы сохранены."
        ),
        "status.saved.title": "Запись сохранена",
        "status.saved.detail": "{filename} ({size:.1f} МБ)",
        "status.saved.no_audio": "{filename} ({size:.1f} МБ) — без звука.",
        "status.recording_error.title": "Ошибка записи",
        "status.recording_error.detail": "{error}",
        "status.recording_error.no_frames": "Не удалось захватить ни одного кадра.",
        "status.folder_error.title": "Не удалось открыть папку",
        "status.folder_error.detail": "{error}",
    },
    "en": {
        "app.title": "Screen Recorder Pro",
        "app.subtitle": "Record a selected area without extra setup",
        "language.tooltip": "Interface language",
        "help.tooltip": "Help and Spider-Man",
        "help.title": "Help from Spider-Man",
        "field.recording_mode": "Recording mode",
        "field.audio": "Audio",
        "button.select_region": "Select area",
        "button.change_region": "Change area",
        "button.start": "Start recording",
        "button.pause": "Pause",
        "button.resume": "Resume",
        "button.stop": "Stop",
        "button.open_folder": "Open folder",
        "shortcuts.available": (
            "Shortcuts:  Ctrl+1 record   •   "
            "Ctrl+2 pause / resume   •   Ctrl+3 stop"
        ),
        "shortcuts.unavailable": (
            "Global shortcuts are unavailable — use the app buttons."
        ),
        "footer.recordings": (
            "Recordings are saved in the “Мои записи” folder next to the app."
        ),
        "quality.tracker": "For sharing - recommended",
        "quality.maximum": "Maximum quality",
        "quality.compact": "Compact size",
        "audio.off": "No audio",
        "audio.system": "System audio",
        "audio.microphone": "Microphone",
        "audio.system_microphone": "System audio and microphone",
        "overlay.select": "Drag to select an area. Esc cancels.",
        "overlay.release": "Release to confirm. Esc cancels.",
        "overlay.too_small": (
            "The area must be at least 2×2 px after even rounding."
        ),
        "status.select.title": "Select a screen area",
        "status.select.initial": "Select the area you want to record.",
        "status.select.overlay": "Press Esc to cancel area selection.",
        "status.ready.title": "Ready to record",
        "status.ready.selected": "Selected area: {width} × {height} px.",
        "status.selection_error.title": "Could not select the area",
        "status.selection_error.detail": "{error}",
        "status.cancelled.empty": (
            "Selection cancelled. The border shows what will be recorded."
        ),
        "status.cancelled.saved": (
            "Selection cancelled. Kept area: {width} × {height} px."
        ),
        "status.no_region.title": "Select a screen area first",
        "status.no_region.detail": (
            "The Start recording button will then become available."
        ),
        "status.start_error.title": "Could not start recording",
        "status.start_error.detail": "{error}",
        "status.recording.title": "Recording",
        "status.recording.detail": (
            "Recording {width} × {height} px at {fps} FPS."
        ),
        "status.paused.title": "Recording paused",
        "status.paused.detail": "Press Ctrl+2 or Resume to continue.",
        "status.resumed.detail": "Recording resumed.",
        "status.saving.title": "Saving video",
        "status.saving.wait": "Please wait.",
        "status.saving.finalize": "Preparing the final MP4.",
        "status.save_error.title": "Save error",
        "status.save_error.no_path": "Temporary video path was not found.",
        "status.save_error.missing_file": (
            "Temporary file was not found: {filename}."
        ),
        "status.save_error.preserved": (
            "{error}. Temporary files were preserved."
        ),
        "status.saved.title": "Recording saved",
        "status.saved.detail": "{filename} ({size:.1f} MB)",
        "status.saved.no_audio": "{filename} ({size:.1f} MB) — no audio.",
        "status.recording_error.title": "Recording error",
        "status.recording_error.detail": "{error}",
        "status.recording_error.no_frames": "No video frames were captured.",
        "status.folder_error.title": "Could not open the folder",
        "status.folder_error.detail": "{error}",
    },
    "it": {
        "app.title": "Screen Recorder Pro",
        "app.subtitle": "Registra un'area selezionata senza configurazioni extra",
        "language.tooltip": "Lingua dell'interfaccia",
        "help.tooltip": "Aiuto e Spider-Man",
        "help.title": "Aiuto da Spider-Man",
        "field.recording_mode": "Modalità di registrazione",
        "field.audio": "Audio",
        "button.select_region": "Seleziona area",
        "button.change_region": "Cambia area",
        "button.start": "Avvia registrazione",
        "button.pause": "Pausa",
        "button.resume": "Riprendi",
        "button.stop": "Termina",
        "button.open_folder": "Apri cartella",
        "shortcuts.available": (
            "Scorciatoie:  Ctrl+1 registra   •   "
            "Ctrl+2 pausa / riprendi   •   Ctrl+3 termina"
        ),
        "shortcuts.unavailable": (
            "Le scorciatoie globali non sono disponibili — usa i pulsanti."
        ),
        "footer.recordings": (
            "Le registrazioni vengono salvate nella cartella “Мои записи” "
            "accanto all'app."
        ),
        "quality.tracker": "Per condividere - consigliato",
        "quality.maximum": "Qualità massima",
        "quality.compact": "Dimensioni compatte",
        "audio.off": "Senza audio",
        "audio.system": "Audio di sistema",
        "audio.microphone": "Microfono",
        "audio.system_microphone": "Audio di sistema e microfono",
        "overlay.select": "Trascina per selezionare un'area. Esc annulla.",
        "overlay.release": "Rilascia per confermare. Esc annulla.",
        "overlay.too_small": (
            "Dopo l'arrotondamento l'area deve essere almeno 2×2 px."
        ),
        "status.select.title": "Seleziona un'area dello schermo",
        "status.select.initial": "Seleziona l'area da registrare.",
        "status.select.overlay": "Premi Esc per annullare la selezione.",
        "status.ready.title": "Pronto per registrare",
        "status.ready.selected": "Area selezionata: {width} × {height} px.",
        "status.selection_error.title": "Impossibile selezionare l'area",
        "status.selection_error.detail": "{error}",
        "status.cancelled.empty": (
            "Selezione annullata. Il bordo mostra l'area da registrare."
        ),
        "status.cancelled.saved": (
            "Selezione annullata. Area mantenuta: {width} × {height} px."
        ),
        "status.no_region.title": "Seleziona prima un'area dello schermo",
        "status.no_region.detail": (
            "Il pulsante Avvia registrazione diventerà disponibile."
        ),
        "status.start_error.title": "Impossibile avviare la registrazione",
        "status.start_error.detail": "{error}",
        "status.recording.title": "Registrazione in corso",
        "status.recording.detail": (
            "Registrazione di {width} × {height} px a {fps} FPS."
        ),
        "status.paused.title": "Registrazione in pausa",
        "status.paused.detail": "Premi Ctrl+2 o Riprendi per continuare.",
        "status.resumed.detail": "Registrazione ripresa.",
        "status.saving.title": "Salvataggio video",
        "status.saving.wait": "Attendi.",
        "status.saving.finalize": "Preparazione del file MP4 finale.",
        "status.save_error.title": "Errore di salvataggio",
        "status.save_error.no_path": (
            "Il percorso del video temporaneo non è stato trovato."
        ),
        "status.save_error.missing_file": (
            "File temporaneo non trovato: {filename}."
        ),
        "status.save_error.preserved": (
            "{error}. I file temporanei sono stati conservati."
        ),
        "status.saved.title": "Registrazione salvata",
        "status.saved.detail": "{filename} ({size:.1f} MB)",
        "status.saved.no_audio": "{filename} ({size:.1f} MB) — senza audio.",
        "status.recording_error.title": "Errore di registrazione",
        "status.recording_error.detail": "{error}",
        "status.recording_error.no_frames": (
            "Non è stato possibile acquisire alcun fotogramma."
        ),
        "status.folder_error.title": "Impossibile aprire la cartella",
        "status.folder_error.detail": "{error}",
    },
}


class _SafeFormatValues(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def normalize_language(language):
    if isinstance(language, str) and language in TRANSLATIONS:
        return language
    return DEFAULT_LANGUAGE


def translate(language, key, **values):
    language = normalize_language(language)
    template = TRANSLATIONS[language].get(key)
    if template is None:
        template = TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)
    return template.format_map(_SafeFormatValues(values))
