"""
Единое разрешение путей к ресурсам, которые нужны и в запуске из исходников,
и в собранном PyInstaller-exe.

- В обычном запуске базой служит папка с исходниками.
- В собранном exe PyInstaller распаковывает данные (папки instruments/, tests/)
  в каталог sys._MEIPASS (для onedir это ...\\_internal). Оттуда и берём
  read-only ресурсы.

Пользовательские данные (CSV/PNG/конфиг) при этом всегда пишутся рядом с
исполняемым файлом/скриптом (см. default_data_dir), а не внутрь _internal.
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


def app_root() -> Path:
    """
    Каталог, рядом с которым логично держать пользовательские данные:
    - для собранного exe — папка с самим exe;
    - для запуска из исходников — папка с проектом.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def default_data_dir() -> Path:
    return app_root() / "data"


def config_dir() -> Path:
    """
    Каталог конфигурации приложения — вне рабочей папки (см. PLAN_V2.md,
    В-1): рабочая папка настраивается оператором и полна CSV/PNG, конфиги
    там не место, их не должно быть случайно легко затереть/переносить
    вместе с результатами измерений.

    На Windows это %LOCALAPPDATA%\\Legoshi\\IDM\\IVTrace\\config — тот же
    путь, что займёт полная схема каталогов инсталлятора (Ф6), просто
    заведённый заранее для того, что нужно уже сейчас (профили датчиков,
    п.39). Если LOCALAPPDATA не задана (не-Windows, некоторые CI-окружения)
    — используем app_root()/config, чтобы функция не падала вообще нигде.
    """
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / "Legoshi" / "IDM" / "IVTrace" / "config"
    return app_root() / "config"


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
    переопределить её из UI (см. set_work_dir); без переопределения —
    default_data_dir() (совместимость с уже привычным поведением при
    запуске из исходников/portable exe — там это папка рядом со скриптом).

    В-1 предполагает для установленного приложения дефолт
    %USERPROFILE%\\Documents\\IVTrace, но инсталлятор (Ф6) ещё не сделан —
    менять сегодняшний дефолт преждевременно, пока некому его выставить
    автоматически при установке. Настраиваемость (сама суть п.23) уже
    работает независимо от того, какой дефолт стоит "из коробки".
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
