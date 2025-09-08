# SitSmartCoach.spec
# Build with:  pyinstaller SitSmartCoach.spec

block_cipher = None

import os
import mediapipe as mp
from PyInstaller.utils.hooks import collect_submodules

# --- Include mediapipe assets (models, graphs, etc.)
mp_dir = os.path.dirname(mp.__file__)
datas = [(mp_dir, "mediapipe")]

# --- Hidden imports (important for mediapipe + cv2 + numpy)
hiddenimports = collect_submodules("mediapipe") + ["cv2", "numpy"]

a = Analysis(
    ['SitSmartCoach.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="SitSmartCoach",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # set True for first test builds to see logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SitSmartCoach",
)

