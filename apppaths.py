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
    """Конфиги мультиметра в роли АМПЕРМЕТРА — выход датчика ТОКА (историческое, основное назначение)."""
    return instruments_dir() / "multimeters"


def voltmeter_cfg_dir() -> Path:
    """
    Конфиги мультиметра в роли ВОЛЬТМЕТРА — выход датчика НАПРЯЖЕНИЯ (п. 4).

    Отдельная директория, а не переключение режима в тех же файлах: один и
    тот же физический прибор (АКИП-2101, АКИП-B7-78/1) описывается двумя
    разными конфигами в зависимости от того, что подключено к его входу —
    другая SCPI-подсистема (VOLT: вместо CURR:), другие шкалы, часто другой
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
