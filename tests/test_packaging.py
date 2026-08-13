"""
Инварианты сборки (ivtrace.spec/installer.spec), которые не проверяются
обычным импортом модуля — только соглашения о том, что и как бандлится.
"""
import re
from pathlib import Path

import pytest

# ivtrace.spec бандлит tests/ в IVTrace.exe для предполётных самотестов
# (см. selftest.run_selftests, run.preflight — гейт перед КАЖДЫМ measure/
# GUI), явно ИСКЛЮЧАЯ эти два файла: они тестируют installer.py/
# installer_core.py — код ОТДЕЛЬНОГО Setup.exe (installer.spec), которого в
# Analysis(["app.py"]) нет и не должно быть.
#
# Баг-репорт: раньше tests/ бандлилась в IVTrace.exe целиком, и самотесты
# падали с ModuleNotFoundError на сборе test_installer.py/
# test_installer_core.py в КАЖДОЙ собранной сборке — предполётная проверка
# была сломана всегда, а не только при поломке кода.
_KNOWN_INSTALLER_TEST_FILES = {"test_installer.py", "test_installer_core.py"}
_INSTALLER_IMPORT_RE = re.compile(
    r'^\s*(import installer\b|from installer\b|import installer_core\b|from installer_core\b)',
    re.MULTILINE,
)


def test_no_untracked_test_files_import_installer_modules():
    """
    Если появится ЕЩЁ один тестовый файл, импортирующий installer/
    installer_core, его нужно добавить в исключения в ivtrace.spec
    (tests_datas) — иначе самотесты в собранном IVTrace.exe снова упадут.
    Этот тест ловит такой рассинхрон ДО сборки.
    """
    tests_dir = Path(__file__).resolve().parent
    offenders = []
    for f in tests_dir.glob("test_*.py"):
        if f.name in _KNOWN_INSTALLER_TEST_FILES:
            continue
        text = f.read_text(encoding='utf-8')
        if _INSTALLER_IMPORT_RE.search(text):
            offenders.append(f.name)
    assert not offenders, (
        f"{offenders} импортируют installer/installer_core, но не исключены "
        f"в ivtrace.spec (tests_datas) — самотесты в собранном IVTrace.exe упадут."
    )


def test_known_installer_test_files_still_import_installer_modules():
    """
    Обратная проверка: если test_installer.py/test_installer_core.py
    когда-нибудь перестанут импортировать installer/installer_core (переезд,
    переименование), исключение в ivtrace.spec станет ненужным мёртвым
    кодом — этот тест напомнит его снять.

    В исходниках оба файла есть всегда. Внутри собранного IVTrace.exe их
    там и не должно быть — они НАМЕРЕННО не бандлятся (см. ivtrace.spec,
    tests_datas) — поэтому здесь это не ошибка, а повод пропустить проверку:
    самотесты внутри exe и так это не увидят своим __file__.
    """
    tests_dir = Path(__file__).resolve().parent
    for name in _KNOWN_INSTALLER_TEST_FILES:
        path = tests_dir / name
        if not path.exists():
            pytest.skip(f"{name} не бандлится в этой сборке (см. ivtrace.spec) — проверка не применима здесь.")
        text = path.read_text(encoding='utf-8')
        assert _INSTALLER_IMPORT_RE.search(text), (
            f"{name} больше не импортирует installer/installer_core — "
            f"проверьте, нужно ли ещё исключать его в ivtrace.spec."
        )
