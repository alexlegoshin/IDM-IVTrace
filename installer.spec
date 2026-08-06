# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller-спецификация сборки Setup.exe (инсталлятор/апдейтер, Ф6).

Сборка:
    pyinstaller installer.spec --noconfirm

Результат: dist/Setup.exe — ОДИН файл (onefile, не onedir как у основного
IVTrace.exe в ivtrace.spec): Setup.exe маленький (Tkinter + стандартная
библиотека, без matplotlib/scipy/pandas/pyvisa), удобнее раздавать одним
файлом, чем тащить отдельную _internal/ папку ради него. Кладётся в архив
релиза рядом с папкой IVTrace/ (см. .github/workflows/release.yml) — при
установке ищет её как app_root()/"IVTrace" (см. installer.py, _payload_dir).

assets/VERSION пишет CI перед сборкой (тег релиза, см. workflow) — здесь
только упаковка того, что уже лежит в assets/ на момент вызова pyinstaller.
"""
datas = [
    ("assets", "assets"),  # баннер, иконка, VERSION (см. installer.py, installer_core.own_build_tag)
]


a = Analysis(
    ["installer.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Setup",
    icon="assets/ivtrace_exe.ico",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # чисто GUI-инсталлятор, без консоли (не CLI)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
