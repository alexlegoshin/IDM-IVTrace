"""
Оркестрация одной сессии измерения: обнаружение/открытие приборов, прогон
двусторонней амплитудной характеристики и запись CSV.

Вынесено отдельно, чтобы один и тот же код использовали и CLI (run.py), и
GUI (gui.py) — без дублирования логики работы с железом. Вся коммуникация с
пользователем идёт через колбэк log(text), по умолчанию — print. Это
позволяет GUI перехватывать прогресс в свой журнал, не меняя ядро.
"""
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from apppaths import (
    multimeter_cfg_dir, current_source_cfg_dir, voltage_source_cfg_dir,
)
from instruments import (
    Multimeter, CurrentSource, VoltageSource,
    discover_instruments, find_config_for_idn,
)
from relay import RelayController, discover_relay_port
from measurement import run_measurement, EXCITATION_UNITS


LogFn = Callable[[str], None]
StopFn = Optional[Callable[[], bool]]


def _resolve_instruments(rm, excitation_type: str, dmm_addr: Optional[str],
                         src_addr: Optional[str], source_cfg_dir: Path,
                         source_label: str, log: LogFn):
    """
    Возвращает (dmm_addr, dmm_cfg, src_addr, src_cfg).

    Если оба адреса заданы вручную — опрашивает *IDN? по каждому, чтобы
    подобрать json-конфиг. Иначе запускает полное автообнаружение.
    """
    if dmm_addr and src_addr:
        log("Открываю приборы по заданным адресам, определяю модели по *IDN?...")

        dmm_instr = rm.open_resource(dmm_addr)
        dmm_instr.encoding = 'utf-8'
        dmm_idn = dmm_instr.query('*IDN?').strip()
        dmm_instr.close()
        dmm_cfg = find_config_for_idn(dmm_idn, multimeter_cfg_dir())
        if dmm_cfg is None:
            raise RuntimeError(f"Не удалось подобрать конфиг мультиметра для IDN: {dmm_idn}")

        src_instr = rm.open_resource(src_addr)
        src_instr.encoding = 'utf-8'
        src_idn = src_instr.query('*IDN?').strip()
        src_instr.close()
        src_cfg = find_config_for_idn(src_idn, source_cfg_dir)
        if src_cfg is None:
            raise RuntimeError(f"Не удалось подобрать конфиг {source_label} для IDN: {src_idn}")

        return dmm_addr, dmm_cfg, src_addr, src_cfg

    # Полное автообнаружение (discover_instruments печатает через print;
    # в GUI это перехватывается редиректом stdout — см. gui.py).
    return discover_instruments(
        multimeter_cfg_dir(), source_cfg_dir, rm=rm, source_label=source_label,
    )


def write_results_csv(csv_path: Path, df: pd.DataFrame, params: dict,
                      excitation_type: str, unit: str) -> None:
    """Пишет CSV с шапкой метаданных (# ...) и данными измерения."""
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write(f"# Датчик: {params['label']}\n")
        f.write(f"# Тип возбуждения: {excitation_type}\n")
        f.write(f"# Единица измерения возбуждения: {unit}\n")
        f.write(f"# Диапазон заданного возбуждения: {params['X_start']}..{params['X_stop']} {unit}, "
                f"шаг {params['X_step']} {unit}\n")
        if excitation_type == 'current':
            f.write(f"# Ограничение напряжения: {params['V_limit']} В\n")
        f.write(f"# Обе полярности сняты автоматически через плату реле (см. колонку Branch)\n")
        f.write(f"# Задержка установки: {params['delay']} с\n")
        f.write(f"# Задержка охлаждения: {params['cooling_delay']} с\n")
        f.write(f"# Время измерения: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Всего точек: {len(df)}\n")
        f.write("#\n")
        df.to_csv(f, index=False)


def run_measurement_session(
    rm,
    params: dict,
    csv_path: Path,
    dmm_addr: Optional[str] = None,
    src_addr: Optional[str] = None,
    relay_port: Optional[str] = None,
    log: LogFn = print,
    should_stop: StopFn = None,
) -> pd.DataFrame:
    """
    Полный цикл: подобрать/открыть приборы и реле, снять обе ветви, записать
    CSV по пути csv_path. Возвращает DataFrame результатов.

    rm             — уже созданный pyvisa.ResourceManager (см. visa_backend).
    params         — словарь параметров из cli.resolve_measure_params.
    dmm/src/relay  — необязательные ручные адреса (иначе автообнаружение).
    log            — колбэк вывода (по умолчанию print).
    should_stop    — колбэк кооперативной остановки (для GUI).

    Гарантирует выключение источника и реле в блоке finally, даже при ошибке.
    """
    excitation_type = params['excitation_type']
    unit = EXCITATION_UNITS[excitation_type]
    source_cfg_dir = current_source_cfg_dir() if excitation_type == 'current' else voltage_source_cfg_dir()
    source_label = "источник тока" if excitation_type == 'current' else "источник напряжения"

    dmm_addr, dmm_cfg, src_addr, src_cfg = _resolve_instruments(
        rm, excitation_type, dmm_addr, src_addr, source_cfg_dir, source_label, log,
    )

    if relay_port:
        log(f"Плата реле: используется заданный порт {relay_port}")
    else:
        relay_port = discover_relay_port()

    dmm = Multimeter(dmm_addr, dmm_cfg, rm=rm)
    src = (CurrentSource(src_addr, src_cfg, rm=rm)
           if excitation_type == 'current'
           else VoltageSource(src_addr, src_cfg, rm=rm))
    relay = RelayController(relay_port)

    log("Приборы и реле инициализированы. Начинаю измерения...")

    try:
        results = run_measurement(
            dmm, src, relay, excitation_type,
            X_start=params['X_start'], X_stop=params['X_stop'], X_step=params['X_step'],
            V_limit=params['V_limit'], delay=params['delay'], cooling_delay=params['cooling_delay'],
            should_stop=should_stop,
        )
    finally:
        dmm.close()
        src.close()
        relay.close()

    log("Измерения завершены, источник и реле выключены.")

    df = pd.DataFrame(results)
    write_results_csv(csv_path, df, params, excitation_type, unit)
    log(f"Данные сохранены в {csv_path}")

    return df
