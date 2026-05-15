# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Photo Reviewer
# Build with:  pyinstaller photo_reviewer.spec

import sys
from pathlib import Path

block_cipher = None

# ── Collect data files ────────────────────────────────────────────────────────
# customtkinter ships theme JSON files that must be bundled
import customtkinter
ctk_path = Path(customtkinter.__file__).parent
ctk_data = [(str(ctk_path), "customtkinter")]

a = Analysis(
    ["photo_reviewer.py"],
    pathex=["."],
    binaries=[],
    datas=ctk_data,
    hiddenimports=[
        # PIL submodules not always auto-detected
        "PIL._tkinter_finder",
        "PIL.Image",
        "PIL.ImageTk",
        "PIL.ImageFilter",
        "PIL.ExifTags",
        # tkinter extras
        "tkinter",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.simpledialog",
        "_tkinter",
        # imagehash internals
        "imagehash",
        "scipy.fftpack",
        "scipy.ndimage",
        # numpy
        "numpy",
        "numpy.core._methods",
        "numpy.lib.format",
        # rawpy
        "rawpy",
        "rawpy._rawpy",
        # send2trash
        "send2trash",
        "send2trash.plat_win",
        # customtkinter
        "customtkinter",
        "customtkinter.windows",
        "customtkinter.windows.widgets",
        "customtkinter.windows.widgets.core_rendering",
        "customtkinter.windows.widgets.core_widget_classes",
        "customtkinter.windows.widgets.font",
        "customtkinter.windows.widgets.appearance_mode",
        "customtkinter.windows.widgets.scaling",
        "customtkinter.windows.widgets.theme",
        "customtkinter.windows.widgets.utility",
        # urllib (used for iNaturalist API)
        "urllib.request",
        "urllib.parse",
        "urllib.error",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy unused packages
        "matplotlib",
        "scipy.spatial",
        "scipy.signal",
        "pandas",
        "IPython",
        "jupyter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "wx",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhotoReviewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no console window — GUI only
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="photo_reviewer_icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="PhotoReviewer",
)
