"""
Единое разрешение путей к ресурсам, которые нужны и в запуске из исходников,
и в собранном PyInstaller-exe.

- В обычном запуске базой служит папка с исходниками.
- В собранном exe PyInstaller распаковывает данные (папки instruments/, tests/)
  в каталог sys._MEIPASS (для onedir это ...\\_internal). Оттуда и берём
  read-only ресурсы.

## Схема каталогов (готовим сейчас под будущий инсталлятор, Ф6)

Оператор при установке выбирает БАЗОВУЮ папку; всё приложение целиком
(exe, конфиги, кэш, рабочая папка по умолчанию) кладётся в
"<выбранная папка>/Legoshi/IVTrace" — одно самодостаточное дерево, без
разнесения конфигов и данных по разным местам диска. См. install_root().

Единственное, что физически НЕ МОЖЕТ жить внутри этого дерева, — запись о
том, где оно находится: искать её было бы негде до того, как мы уже знаем,
где искать. Эта запись (install_root + версия установленного билда — для
будущего апдейтера, чтобы сравнивать версии) хранится СНАРУЖИ, в
%LOCALAPPDATA% (см. read_install_record/write_install_record) — пишет и
читает её будущий инсталлятор/апдейтер; сегодня, без инсталлятора, этой
записи попросту нет, и всё резолвится в app_root() — папку со
скриптом/exe, как и раньше.
"""
import json
import os
import sys
from pathlib import Path
from typing import Optional

# Версия приложения — используется, например, в метаданных сохранённых
# профилей датчиков (см. config.SensorConfigManager, п.39), чтобы было
# видно, каким билдом сохранён конкретный профиль.
APP_VERSION = "2.0-dev"


def resource_base() -> Path:
    """Каталог с упакованными read-only ресурсами (instruments/, tests/)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def instruments_dir() -> Path:
    return resource_base() / "instruments"


def multimeter_cfg_dir() -> Path:
    """Конфиги мультиметра в роли АМПЕРМЕТРА — выход датчика ТОКА (историческое, основное назначение)."""
    return instruments_dir() / "multimeters_current"


def voltmeter_cfg_dir() -> Path:
    """
    Конфиги мультиметра в роли ВОЛЬТМЕТРА — выход датчика НАПРЯЖЕНИЯ (п. 4).

    Отдельная директория (симметрично multimeters_current/), а не
    переключение режима в тех же файлах: один и тот же физический прибор
    (АКИП-2101, АКИП-B7-78/1) описывается двумя разными конфигами в
    зависимости от того, что подключено к его входу — другая SCPI-
    подсистема (VOLT: вместо CURR:), другие шкалы, часто другой
    measure_command. find_config_for_idn() ищет по ключевым словам внутри
    ОДНОЙ директории, поэтому смешивать амперметровые и вольтметровые
    конфиги в одном каталоге сделало бы автообнаружение неоднозначным —
    непонятно, какой из двух конфигов одного и того же IDN выбрать.
    """
    return instruments_dir() / "multimeters_voltage"


def current_source_cfg_dir() -> Path:
    return instruments_dir() / "current_sources"


def voltage_source_cfg_dir() -> Path:
    return instruments_dir() / "voltage_sources"


def tests_dir() -> Path:
    return resource_base() / "tests"


def assets_dir() -> Path:
    """Логотип/иконки (см. gui.py) — read-only ресурс, той же природы, что instruments_dir()/tests_dir()."""
    return resource_base() / "assets"


def app_root() -> Path:
    """
    Каталог, рядом с которым логично держать пользовательские данные:
    - для собранного exe — папка с самим exe;
    - для запуска из исходников — папка с проектом.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# ----------------------------------------------------------------------
# Запись "куда установлено ПО" (Ф6, будущий инсталлятор/апдейтер) —
# единственное, что хранится СНАРУЖИ install_root() (см. докстринг модуля).
# Сегодня ничего эту запись не пишет — только читает (install_root()) и
# готова принять запись, когда появится сам инсталлятор.
# ----------------------------------------------------------------------

def _install_record_path() -> Path:
    """
    %LOCALAPPDATA%\\Legoshi\\IVTrace\\install_info.json — фиксированное
    место, не зависящее от того, куда оператор в итоге поставил само
    приложение (иначе искать эту запись было бы негде). На не-Windows
    (LOCALAPPDATA не задана — macOS/Linux, CI) падать не должно нигде,
    поэтому используем app_root() как запасной корень — там всё равно
    никто и не будет писать эту запись без реального инсталлятора.
    """
    local_app_data = os.environ.get('LOCALAPPDATA')
    base = Path(local_app_data) if local_app_data else app_root()
    return base / "Legoshi" / "IVTrace" / "install_info.json"


def read_install_record() -> Optional[dict]:
    """
    {'install_root': str, 'tag': str, 'release_date': Optional[str]} — куда
    установлено ПО, из какого релиза GitHub (тег + ISO-дата публикации,
    installer_core.fetch_releases) и когда — апдейтеру (Ф6, installer.py)
    нужны обе величины: тег для обычного сравнения версий, дата — для
    редкого случая "тот же тег, но релиз на GitHub переопубликован заново"
    (см. installer_core.is_newer). None, если записи нет или она
    повреждена. Отсутствие записи — штатный случай (запуск из исходников
    или portable exe без инсталлятора), а не ошибка.
    """
    path = _install_record_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def write_install_record(root: Path, tag: str, release_date: Optional[str] = None) -> None:
    """
    Пишет запись "куда установлено ПО" (см. _install_record_path). Вызывает
    инсталлятор/апдейтер (installer.py, Ф6). `tag` — версия релиза GitHub
    (например "v2.0"), НЕ apppaths.APP_VERSION (тот — отдельный, для меток
    в профилях датчиков, см. config.py). `release_date` может быть
    неизвестна (офлайн-установка из уже скачанного архива без обращения к
    GitHub API) — тогда None, и апдейтер сравнивает только по тегу.
    """
    path = _install_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {'install_root': str(Path(root)), 'tag': tag, 'release_date': release_date},
            indent=4, ensure_ascii=False,
        ),
        encoding='utf-8',
    )


def install_root() -> Path:
    """
    Корень, под которым живёт ВСЁ — конфиги, кэш, рабочая папка по
    умолчанию (см. докстринг модуля). Если инсталлятор оставил запись (см.
    read_install_record) и путь из неё реально существует на диске —
    используем его. Иначе (нет записи, или записанный путь исчез —
    приложение переставили руками) — app_root(), в точности прежнее
    поведение при запуске из исходников/portable exe.
    """
    record = read_install_record()
    if record:
        candidate = record.get('install_root')
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return app_root()


def default_data_dir() -> Path:
    return install_root() / "data"


def config_dir() -> Path:
    """
    Каталог конфигурации приложения — под install_root() (см. докстринг
    модуля), рядом с рабочей папкой по умолчанию (default_data_dir), но
    независимо от неё: рабочую папку оператор может переопределить куда
    угодно (см. work_dir/set_work_dir, п.23) — конфиги от этого переезда
    не зависят и не должны потеряться вместе с результатами измерений
    (см. PLAN_V2.md, В-1).
    """
    return install_root() / "config"


def sensor_config_dir() -> Path:
    """Корень профилей датчиков (см. config.SensorConfigManager, п.39) — подпапки current/voltage внутри."""
    return config_dir() / "sensors"


# ----------------------------------------------------------------------
# Рабочая папка (п.23) — настраивается из UI, хранится отдельно от самих
# CSV/PNG/XLSX: сама настройка — это конфигурация приложения (в config_dir),
# а не результат измерения.
# ----------------------------------------------------------------------

def _app_settings_path() -> Path:
    return config_dir() / "app_settings.json"


def load_app_settings() -> dict:
    path = _app_settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def save_app_settings(settings: dict) -> None:
    path = _app_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=4, ensure_ascii=False), encoding='utf-8')


def work_dir() -> Path:
    """
    Рабочая папка результатов измерений (п.23) — оператор может
    переопределить её из UI на что угодно (см. set_work_dir) — эта
    настраиваемость никак не урезается новой схемой каталогов. Без
    переопределения — default_data_dir(), то есть install_root()/data:
    результаты по умолчанию лежат внутри того же дерева, что и всё
    остальное (см. докстринг модуля), а не где-то отдельно.
    """
    override = load_app_settings().get('work_dir')
    if override:
        return Path(override)
    return default_data_dir()


def set_work_dir(path: Optional[Path]) -> None:
    """path=None сбрасывает переопределение (снова default_data_dir())."""
    settings = load_app_settings()
    if path is None:
        settings.pop('work_dir', None)
    else:
        settings['work_dir'] = str(Path(path))
    save_app_settings(settings)


def cache_dir() -> Path:
    """<рабочая папка>\\Cache (п.23, В-1)."""
    return work_dir() / "Cache"


def clear_results_cache() -> list:
    """
    Удаляет накопленные результаты измерений (CSV/PNG/XLSX прямо в
    work_dir(), включая *_inverted.csv — все они начинаются с "IVtrace_")
    и файл параметров последнего запуска (ivtrace_config.json), плюс
    целиком очищает <рабочая папка>\\Cache. Не трогает конфиги приборов
    (instruments_dir()) и профили датчиков (sensor_config_dir()) — они
    физически в других директориях (config_dir()), глоб по work_dir() их
    не видит вовсе.

    Возвращает список удалённых путей — для лога в UI.
    """
    removed = []
    base = work_dir()
    if base.is_dir():
        for pattern in ("IVtrace_*.csv", "IVtrace_*.png", "IVtrace_*.xlsx"):
            for p in sorted(base.glob(pattern)):
                p.unlink()
                removed.append(p)
        last_run = base / "ivtrace_config.json"
        if last_run.exists():
            last_run.unlink()
            removed.append(last_run)

    cache = cache_dir()
    if cache.is_dir():
        for p in sorted(cache.rglob("*")):
            if p.is_file():
                p.unlink()
                removed.append(p)
        for p in sorted(cache.glob("**/*"), reverse=True):
            if p.is_dir():
                p.rmdir()

    return removed
