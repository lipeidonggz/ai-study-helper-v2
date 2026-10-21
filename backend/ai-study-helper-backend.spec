# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


backend_dir = Path(SPECPATH)
hidden_imports = [
    module
    for module in (
        collect_submodules("uvicorn")
        + collect_submodules("fastapi")
        + collect_submodules("pydantic")
    )
    if "uvloop" not in module
]

a = Analysis(
    [str(backend_dir / "app" / "main.py")],
    pathex=[str(backend_dir)],
    binaries=[],
    datas=[(str(backend_dir / "migrations"), "migrations")],
    hiddenimports=hidden_imports,
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
    [],
    exclude_binaries=True,
    name="ai-study-helper-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
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
    upx=False,
    upx_exclude=[],
    name="ai-study-helper-backend",
)
