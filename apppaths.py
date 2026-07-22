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
import sys
from pathlib import Path


def resource_base() -> Path:
    """Каталог с упакованными read-only ресурсами (instruments/, tests/)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def instruments_dir() -> Path:
    return resource_base() / "instruments"


def multimeter_cfg_dir() -> Path:
    return instruments_dir() / "multimeters"


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
