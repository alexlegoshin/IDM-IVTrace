"""
Предполётные самотесты.

Идея: перед любой работой с реальным железом (команда measure в CLI и старт
измерения в GUI) прогнать быстрый виртуальный тест-набор из tests/. Если код
повреждён/рассогласован — тесты падают, и программа отказывается управлять
приборами. Это не панацея (тесты работают на заглушках, а не на живом
железе), но дешёвая страховка от очевидных поломок логики, способных
повредить оборудование.

Запуск pytest делается программно (pytest.main), одинаково из исходников и из
собранного PyInstaller-exe (тесты кладутся в сборку как данные, путь берётся
из apppaths.tests_dir). Флаги подобраны так, чтобы не зависеть от прав на
запись рядом с exe и от переписывания assert'ов.
"""
import contextlib
import io
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from apppaths import tests_dir, resource_base


@dataclass
class SelfTestResult:
    ok: bool
    returncode: int
    output: str
    summary: str


def _summarize(output: str) -> str:
    """Достаёт из вывода pytest последнюю содержательную строку-итог."""
    for line in reversed(output.strip().splitlines()):
        s = line.strip().strip("=").strip()
        if s:
            return s
    return "нет вывода pytest"


def run_selftests(verbose: bool = False) -> SelfTestResult:
    """
    Прогоняет тест-набор из tests/. Возвращает SelfTestResult; исключений
    наружу не бросает (любой сбой самого запуска трактуется как провал).
    """
    tdir = tests_dir()
    if not tdir.exists():
        return SelfTestResult(
            ok=False, returncode=-1, output="",
            summary=f"Каталог тестов не найден: {tdir}",
        )

    try:
        import pytest  # noqa: F401
    except Exception as e:
        return SelfTestResult(
            ok=False, returncode=-1, output="",
            summary=f"pytest недоступен, самопроверка невозможна: {e}",
        )

    # tests/ импортирует пакет tests.conftest — база ресурсов должна быть на
    # sys.path, чтобы 'import tests...' разрешался и в собранном exe.
    base = str(resource_base())
    if base not in sys.path:
        sys.path.insert(0, base)

    # В собранном exe автозагрузка сторонних pytest-плагинов через
    # entry points ненадёжна; наши тесты используют только встроенные
    # фикстуры (tmp_path/monkeypatch/capsys), поэтому отключаем автозагрузку.
    os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")

    args = [
        str(tdir),
        "-p", "no:cacheprovider",   # не пытаться писать .pytest_cache рядом с exe
        "--assert=plain",           # без переписывания assert (нужна запись в __pycache__)
        "-q",
    ]
    if not verbose:
        args.append("--no-header")

    buf = io.StringIO()
    # Изолируем возможные временные файлы pytest в системный temp.
    with tempfile.TemporaryDirectory(prefix="ivtrace_selftest_") as tmp:
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                returncode = int(pytest.main(args))
        except SystemExit as e:  # pytest иногда завершает через SystemExit
            returncode = int(e.code) if e.code is not None else 1
        except Exception as e:
            return SelfTestResult(
                ok=False, returncode=-1, output=buf.getvalue(),
                summary=f"Сбой запуска самотестов: {e}",
            )
        finally:
            os.chdir(old_cwd)

    output = buf.getvalue()
    return SelfTestResult(
        ok=(returncode == 0),
        returncode=returncode,
        output=output,
        summary=_summarize(output),
    )
