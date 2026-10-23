# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: freeze the AnkiSense FastAPI backend into a standalone exe.

Run from desktop/ with the project venv:
    ..\\.venv\\Scripts\\python.exe -m PyInstaller ankisense-backend.spec --noconfirm --clean

Output lands in desktop/dist/ankisense-backend/, which electron-builder copies
into the installer's resources via the "extraResources" entry in package.json.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

PROJECT = Path(SPECPATH).resolve().parent  # repo root (spec lives in desktop/)

datas = [
    # app.py resolves STATIC as <frozen root>/ankisense/static, so keep that
    # relative layout in the bundle (no backend source change needed).
    (str(PROJECT / "ankisense" / "static"), "ankisense/static"),
]
binaries = []
hiddenimports = []

# rapidocr ships its ONNX models + config yaml next to its own package (located
# via __file__), and onnxruntime ships native DLLs. collect_all grabs code,
# data and dynamic libraries for both.
for package in ("rapidocr", "onnxruntime"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["backend_launcher.py"],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "paddle", "openvino", "tensorrt", "mnn", "torch"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ankisense-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ankisense-backend",
)
