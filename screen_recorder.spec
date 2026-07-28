# -*- mode: python ; coding: utf-8 -*-

import shutil
from pathlib import Path


project_dir = Path(SPECPATH)
icon_path = project_dir / 'screen-recorder-icon.ico'
if not icon_path.is_file():
    icon_path = project_dir / 'assets' / 'screen-recorder-icon.ico'
if not icon_path.is_file():
    raise RuntimeError('screen-recorder-icon.ico was not found')

ffmpeg_path = shutil.which('ffmpeg')
if not ffmpeg_path:
    raise RuntimeError('ffmpeg.exe is required to build the portable recorder')

a = Analysis(
    ['screen_recorder.py'],
    pathex=[],
    binaries=[(ffmpeg_path, '.')],
    datas=[
        ('spider-man-dance.gif', '.'),
        (str(icon_path), '.'),
        ('assets/chevron-down.svg', '.'),
    ],
    hiddenimports=[
        'pyaudiowpatch',
        '_portaudiowpatch',
        'soundcard',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='screen_recorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path),
)
