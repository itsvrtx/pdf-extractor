# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for a single-file Windows executable.
#   pyinstaller --noconfirm --clean pdf_extractor.spec
#
# Notes
#  * collect_all() pulls in each package's data files. This matters for:
#      - playwright    -> its bundled Node driver (driver/node.exe + package/)
#      - customtkinter -> its theme JSON + assets
#      - rich/pypdf    -> dynamically imported submodules
#  * The exe drives the Edge/Chrome already installed on the PC, so no
#    browser is bundled (keeps the exe around 100 MB instead of 250+).
#  * console=False gives a clean GUI app. Set console=True if you want CLI output
#    (e.g. PDFExtractor.exe https://site --deep) to print in a terminal.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

# Icon: drop your file at assets/app.ico (see assets/README.txt). It becomes the .exe icon AND is
# bundled so the running app can set its own window/taskbar icon.
ICON = Path(SPECPATH) / "assets" / "app.ico"

datas, binaries, hiddenimports = [], [], []
if ICON.exists():
    datas.append((str(ICON), "assets"))
else:
    print("WARNING: assets/app.ico not found - building with the default icon.")
for pkg in ("playwright", "customtkinter", "pypdf", "rich", "cryptography"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["pdf_extractor.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "scipy", "PyQt5", "PySide6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PDFExtractor",
    debug=False,
    strip=False,
    upx=False,            # UPX often trips antivirus on Playwright's node.exe
    runtime_tmpdir=None,
    console=False,
    icon=str(ICON) if ICON.exists() else None,
)
