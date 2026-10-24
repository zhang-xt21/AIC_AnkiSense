# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: freeze the AnkiSense FastAPI backend into a standalone exe.

Run from desktop/ with the project venv:
    ..\\.venv\\Scripts\\python.exe -m PyInstaller ankisense-backend.spec --noconfirm --clean

Output lands in desktop/dist/ankisense-backend/, which electron-builder copies
into the installer's resources via the "extraResources" entry in package.json.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

PROJECT = Path(SPECPATH).resolve().parent  # repo root (spec lives in desktop/)

datas = [
    # app.py resolves STATIC as <frozen root>/ankisense/static, so keep that
    # relative layout in the bundle (no backend source change needed).
    (str(PROJECT / "ankisense" / "static"), "ankisense/static"),
    (str(PROJECT / "ankisense" / "workers"), "ankisense/workers"),
]
# Package only distributable add-on files, never developer bytecode or local metadata.
datas += [(str(PROJECT / "addons" / "ankisense_sync" / name), "addons/ankisense_sync") for name in (
    "__init__.py", "ui.py", "onboarding.py", "answer_ui.py", "answer_journal.py", "client.py",
    "transport.py", "collection_sync.py", "manifest.json", "config.json", "config.md", "bootstrap.apkg",
)]
binaries = []
hiddenimports = collect_submodules("uvicorn")

# rapidocr ships its ONNX models + config yaml next to its own package (located
# via __file__), and onnxruntime ships native DLLs. collect_all grabs code,
# data and dynamic libraries for both.
for package in ("rapidocr", "onnxruntime"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h

# OCR reports package versions via importlib.metadata even in a frozen build.
for package in ("rapidocr", "onnxruntime", "pypdfium2", "pillow"):
    datas += copy_metadata(package)

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
