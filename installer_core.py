"""
Чистая логика инсталлятора/апдейтера (Setup.exe, Ф6) — без Tk, без прямых
побочных эффектов на сеть/диск, кроме тех функций, что явно их делают
(fetch_releases, copy_payload, create_shortcut). Отдельно от installer.py
(Tk-обвязка, состояния, диалоги) по тому же принципу, что sweep.py/
measurement.py отделены от gui.py — здесь то, что можно и нужно покрыть
тестами без реального Windows/сети/Tk.

Источник обновлений — GitHub Releases текущего репозитория. Ищем среди
НЕ draft и НЕ prerelease релизов (см. select_latest_release) тот, что новее
установленного — по тегу, а если тег совпадает (или совпадает числовая
часть тега, см. parse_tag) — по дате публикации (см. is_newer): так
ловится редкий случай "тег тот же, но релиз на GitHub пересобрали и
переопубликовали заново".
"""
import json
import re
import shutil
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

GITHUB_API_RELEASES_URL = "https://api.github.com/repos/alexlegoshin/IDM-IVTrace/releases"

_TAG_RE = re.compile(r'^[vV]?(\d+(?:\.\d+)*)')
_WINDOWS_ASSET_RE = re.compile(r'\bwin(?:32|64)?\b|\bwindows\b', re.IGNORECASE)


def parse_tag(tag: str) -> Optional[Tuple[int, ...]]:
    """
    "v2.0" -> (2, 0); "v2.10.1" -> (2, 10, 1); суффиксы вроде "-alpha"/
    "-beta" игнорируются (сравнение числовой части, см. is_newer — при
    равных числовых частях решает дата публикации, а не текст суффикса).
    None, если строка не начинается с числа (после необязательной "v").
    """
    if not tag:
        return None
    m = _TAG_RE.match(tag.strip())
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split('.'))


def _parse_iso(date_str: str) -> datetime:
    # GitHub отдаёт даты с суффиксом "Z" — fromisoformat принимает его
    # только с Python 3.11, а среда сборки CI может отличаться от локальной.
    return datetime.fromisoformat(date_str.replace('Z', '+00:00'))


def _date_is_newer(remote_date: Optional[str], local_date: Optional[str]) -> bool:
    if not remote_date or not local_date:
        return False
    try:
        return _parse_iso(remote_date) > _parse_iso(local_date)
    except ValueError:
        return False


def is_newer(remote_tag: str, remote_date: Optional[str],
             local_tag: str, local_date: Optional[str]) -> bool:
    """
    remote новее local? Тег сравнивается численно (parse_tag); если числовые
    части равны (в том числе когда сами строки тегов совпадают буквально —
    например, релиз на GitHub переопубликован под тем же тегом) — решает
    дата публикации. Если тег не распознан ни у одного из двух — не гадаем,
    считаем что не новее (безопасный дефолт: лучше пропустить обновление,
    чем ошибочно предложить его на нераспознанных данных).
    """
    if remote_tag == local_tag:
        return _date_is_newer(remote_date, local_date)
    rv, lv = parse_tag(remote_tag), parse_tag(local_tag)
    if rv is None or lv is None:
        return False
    if rv != lv:
        return rv > lv
    return _date_is_newer(remote_date, local_date)


def is_windows_asset(filename: str) -> bool:
    """"win"/"Win"/"WIN"/"Windows"/"win64" и т.п. — без учёта регистра, по
    границе слова (не срабатывает на случайных подстроках типа "darwin")."""
    return bool(_WINDOWS_ASSET_RE.search(filename or ''))


def pick_windows_asset(release: dict) -> Optional[dict]:
    """Первый ассет релиза, чьё имя похоже на Windows-сборку, либо None."""
    for asset in release.get('assets') or []:
        if is_windows_asset(asset.get('name', '')):
            return asset
    return None


def select_latest_release(releases: list) -> Optional[dict]:
    """
    Среди релизов (ответ GitHub API /releases, список от новых к старым)
    выбирает самый новый (см. is_newer) НЕ draft и НЕ prerelease релиз, у
    которого есть хотя бы один Windows-ассет. Пре-релизы (v2.0-alpha/beta
    и т.п. из PLAN_V2.md, §5) участвуют в сравнении ровно как обычные
    релизы, если явно не помечены на GitHub как draft/prerelease — это
    статус GitHub, а не текст в теге.
    """
    best = None
    for release in releases or []:
        if release.get('draft') or release.get('prerelease'):
            continue
        if pick_windows_asset(release) is None:
            continue
        if best is None or is_newer(
            release.get('tag_name', ''), release.get('published_at'),
            best.get('tag_name', ''), best.get('published_at'),
        ):
            best = release
    return best


def own_build_tag() -> Optional[str]:
    """
    Тег, из которого собран ЭТОТ Setup.exe — пишет CI при сборке в
    assets/VERSION (см. .github/workflows/release.yml). None при запуске
    не из релизной сборки (дев-режим, assets/VERSION нет).
    """
    from apppaths import assets_dir
    path = assets_dir() / "VERSION"
    if not path.exists():
        return None
    text = path.read_text(encoding='utf-8').strip()
    return text or None


def fetch_releases(timeout: float = 4.0) -> list:
    """
    GET .../releases — полный список (не только /latest, см. модульный
    докстринг). Любая сетевая/HTTP/JSON ошибка пробрасывается наружу как
    есть — вызывающий код (installer.py) сам решает, что с этим делать
    (тихий фолбэк при отсутствии интернета, без единого уведомления).
    """
    request = urllib.request.Request(
        GITHUB_API_RELEASES_URL,
        headers={"User-Agent": "IVTrace-Setup", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode('utf-8'))
    if not isinstance(data, list):
        raise ValueError("Неожиданный ответ GitHub API: ожидался список релизов.")
    return data


def copy_payload(src_dir: Path, dest_dir: Path) -> None:
    """Копирует содержимое src_dir (папка IVTrace/ рядом с Setup.exe) в dest_dir (install_root), с перезаписью."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)


def create_shortcut(target_exe: Path, shortcut_path: Path, working_dir: Optional[Path] = None) -> None:
    """
    Создаёт .lnk через PowerShell (COM WScript.Shell) — без новой Python-
    зависимости (pywin32 не добавляем), тот же инструмент, что уже
    использует build_windows.ps1. Иконка берётся из самого target_exe
    (см. ivtrace.spec, EXE(icon=...)) — Explorer подтягивает её сам, менять
    отдельно не нужно.
    """
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = working_dir or target_exe.parent
    script = (
        f'$s = (New-Object -ComObject WScript.Shell).CreateShortcut("{shortcut_path}"); '
        f'$s.TargetPath = "{target_exe}"; '
        f'$s.WorkingDirectory = "{work_dir}"; '
        f'$s.Save()'
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
