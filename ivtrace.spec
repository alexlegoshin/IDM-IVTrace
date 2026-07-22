# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller-спецификация сборки IVTrace в portable-приложение (onedir).

Сборка:
    pyinstaller ivtrace.spec --noconfirm

Результат: dist/IVTrace/IVTrace.exe (+ папка _internal). Копируется целиком
как portable-папка; установка Python не требуется. NI-VISA на целевом ПК
всё равно нужна (см. README) — это системный драйвер, в exe он не входит.

Один консольный exe обслуживает и GUI (без аргументов), и CLI (measure/
analyze) — приложение намеренно не дробится.
"""
from PyInstaller.utils.hooks import collect_submodules

# Встроенные плагины pytest нужны для предполётных самотестов внутри exe.
hidden = ["pytest"] + collect_submodules("_pytest")

# Данные (read-only ресурсы), которые ищутся через apppaths по sys._MEIPASS.
datas = [
    ("instruments", "instruments"),   # json-конфиги приборов
    ("tests", "tests"),               # виртуальные самотесты (гейт перед measure)
]

# pyvisa-py в поставку не входит: работаем строго через NI-VISA. Исключаем,
# чтобы случайно установленный на dev-машине пакет не попал в сборку и не
# маскировал отсутствие NI-VISA на целевом ПК.
excludes = ["pyvisa_py"]


a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="IVTrace",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # консоль нужна для CLI; GUI открывает своё окно поверх
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
    name="IVTrace",
)
