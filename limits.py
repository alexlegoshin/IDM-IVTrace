"""
Токовые пределы платы реле стенда.

Это ограничение самой платы реле, а не источника тока: источник на стенде
(АКИП-1162-10-1020, см. instruments/current_sources/akip1162.json) способен
выдать 1020 А — больше, чем держит реле. Значит для тока именно предел реле
является связывающим ограничением, и проверять нужно его, а не паспорт
источника.

RELAY_MAX_CURRENT_A — жёсткий запрет. Выше него измерение не запускается
никогда, ни при каком подтверждении. Разработчик не проверял и не может
проверить поведение платы за этой границей.

RELAY_WARN_CURRENT_A — по мануалу производителя стенда реле рассчитана на
такой ток; по факту начинки работает и выше, вплоть до RELAY_MAX_CURRENT_A,
но это уже сверх паспорта, и ответственность за реле в этом диапазоне на
операторе.

ВАЖНО (см. PLAN_V2.md, п. 37 — витки через датчик): проверять эти пределы
нужно по уставке, которая реально идёт через реле и провод, а НЕ по
"уставка × число витков". Витки умножают ампервитки внутри датчика, а не
ток в проводе — проверка по произведению запретила бы штатный промер
2000 А датчика четырьмя витками при безопасных 500 А в проводе. Пока в
коде нет параметра числа витков (появится в Ф2), max_relay_current()
вызывающей стороне следует передавать именно ток через провод.
"""
import json
from pathlib import Path
from typing import Dict, Optional

RELAY_MAX_CURRENT_A = 800.0
RELAY_WARN_CURRENT_A = 400.0

RELAY_LIABILITY_NOTICE = (
    "По мануалу производителя стенда реле не рассчитана на ток свыше "
    f"{RELAY_WARN_CURRENT_A:.0f} А, но по факту начинки работает вплоть до "
    f"{RELAY_MAX_CURRENT_A:.0f} А. Разработчик ПО не несёт ответственности "
    "за состояние реле при работе на токах свыше "
    f"{RELAY_WARN_CURRENT_A:.0f} А, если она сгорит."
)


def relay_current_block_reason(max_abs_current_a: Optional[float]) -> Optional[str]:
    """
    Возвращает текст причины отказа, если max_abs_current_a превышает
    жёсткий запрет платы реле, иначе None.

    max_abs_current_a — наибольший по модулю ток, который реально пройдёт
    через провод и реле за время измерения (не через датчик с учётом
    витков).
    """
    if max_abs_current_a is None:
        return None
    if max_abs_current_a > RELAY_MAX_CURRENT_A:
        return (
            f"Уставка тока {max_abs_current_a:.1f} А превышает жёсткий предел "
            f"платы реле ({RELAY_MAX_CURRENT_A:.0f} А). Измерение с таким током "
            "запрещено безусловно — свыше этой границы поведение платы не "
            "проверялось."
        )
    return None


def relay_current_warning(max_abs_current_a: Optional[float]) -> Optional[str]:
    """
    Возвращает предупреждающий текст, если max_abs_current_a превышает
    паспортный предел (но не жёсткий запрет), иначе None.
    """
    if max_abs_current_a is None:
        return None
    if RELAY_WARN_CURRENT_A < max_abs_current_a <= RELAY_MAX_CURRENT_A:
        return (
            f"Уставка тока {max_abs_current_a:.1f} А превышает паспортный предел "
            f"реле ({RELAY_WARN_CURRENT_A:.0f} А). {RELAY_LIABILITY_NOTICE}"
        )
    return None


def strictest_current_source_limits(config_dir: Optional[Path] = None) -> Dict[str, Optional[float]]:
    """
    Возвращает {'max_current': ..., 'max_voltage': ...} — минимум паспортных
    пределов среди всех сконфигурированных источников тока.

    Это отдельный от реле предел: у АКИП-1162-10-1020 паспортные 10 В/1020 А
    (см. instruments/current_sources/akip1162.json), и задание, скажем,
    V_limit = 15 В физически недостижимо независимо от реле — источник
    выше 10 В не поднимется. На момент проверки параметров ввода ещё
    неизвестно, какой конкретно источник подключится (автообнаружение
    происходит позже, при открытии приборов), поэтому берётся минимум по
    всем известным конфигам — консервативная граница, которая не пропустит
    значение, недостижимое ни на одном из них.

    Значения None для ключа означают "предел не заявлен ни в одном
    конфиге" — тогда соответствующая проверка в cli.validate_measure_params
    просто не выполняется, а не трактуется как "предела нет".
    """
    if config_dir is None:
        from apppaths import current_source_cfg_dir
        config_dir = current_source_cfg_dir()

    max_currents = []
    max_voltages = []
    for json_file in sorted(Path(config_dir).glob("*.json")):
        try:
            cfg = json.loads(json_file.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue
        if 'max_current' in cfg:
            max_currents.append(cfg['max_current'])
        if 'max_voltage' in cfg:
            max_voltages.append(cfg['max_voltage'])

    return {
        'max_current': min(max_currents) if max_currents else None,
        'max_voltage': min(max_voltages) if max_voltages else None,
    }
