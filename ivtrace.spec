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
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# Встроенные плагины pytest нужны для предполётных самотестов внутри exe.
hidden = ["pytest"] + collect_submodules("_pytest")

# tests/ бандлится поштучно, а не целой папкой — БЕЗ test_installer.py/
# test_installer_core.py. Баг-репорт: эти два файла тестируют installer.py/
# installer_core.py — код ОТДЕЛЬНОГО Setup.exe (см. installer.spec), которого
# в Analysis(["app.py"]) ниже нет и не должно быть (Setup.exe специально
# собирается отдельным, лёгким exe). Раньше tests/ бандлилась целиком
# ("tests", "tests"), и предполётные самотесты (selftest.run_selftests —
# гейт перед КАЖДЫМ measure/GUI, см. run.preflight) падали на сборе тестов
# с ModuleNotFoundError на этих двух файлах в любой собранной сборке —
# самотесты были сломаны, а не просто предупреждали.
_excluded_test_files = {"test_installer.py", "test_installer_core.py"}
tests_datas = [
    (str(f), "tests")
    for f in sorted(Path("tests").glob("*.py"))
    if f.name not in _excluded_test_files
]

# Данные (read-only ресурсы), которые ищутся через apppaths по sys._MEIPASS.
datas = [
    ("instruments", "instruments"),   # json-конфиги приборов
    *tests_datas,                     # виртуальные самотесты (гейт перед measure)
    ("assets", "assets"),             # логотип/иконки GUI (см. gui.py, apppaths.assets_dir)
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
    icon="assets/ivtrace_exe.ico",   # иконка exe/ярлыка (Ф6, см. assets/ivtrace_exe.ico)
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
