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
    multimeter_cfg_dir, voltmeter_cfg_dir, current_source_cfg_dir, voltage_source_cfg_dir,
)
from instruments import (
    Multimeter, CurrentSource, VoltageSource,
    discover_instruments, find_config_for_idn,
)
from relay import RelayController, discover_relay_port
from measurement import (
    run_measurement, EXCITATION_UNITS, OUTPUT_UNITS,
    DEFAULT_AVERAGING_COUNT, DEFAULT_AVERAGING_DELAY, DEFAULT_DISCARD_FIRST,
    DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER,
)
from sweep import Branch, DirectionPreset
from safety import emergency_shutdown
from calibration import CalibrationStatus, check_calibration


LogFn = Callable[[str], None]
StopFn = Optional[Callable[[], bool]]


class SessionHandle:
    """
    Ручка на открытые приборы текущей сессии.

    Нужна ровно для одного: дать возможность обесточить стенд **снаружи**
    измерительного цикла — из обработчика кнопки «Стоп», который живёт в
    UI-потоке, пока рабочий поток занят внутри run_measurement().

    Без такой ручки остановка может быть только кооперативной: выставить
    флаг и ждать, пока цикл сам доберётся до проверки между точками. На
    стенде с сотнями ампер это ожидание недопустимо — оператор нажимает
    «Стоп», когда что-то идёт не так прямо сейчас.

    О потоках. emergency_stop() намеренно выполняется в потоке вызывающего,
    а не передаётся в рабочий: смысл аварийного останова в том, что он не
    ждёт рабочий поток. Рабочий поток в этот момент почти всегда либо спит
    (задержки установки/охлаждения), либо читает мультиметр — то есть занят
    другой VISA-сессией, не той, в которую пишет аварийная
    последовательность (источник) и не портом реле. Риск наложения на
    редкую запись в источник сознательно принят: разомкнутое реле важнее
    аккуратности VISA-обмена.
    """

    def __init__(self):
        self.dmm = None
        self.src = None
        self.relay = None
        self.stopped = False

    def emergency_stop(self, log: Optional[LogFn] = None):
        """Обесточивает стенд немедленно. Повторные вызовы безвредны."""
        self.stopped = True
        return emergency_shutdown(src=self.src, relay=self.relay, dmm=self.dmm, log=log)


def _resolve_instruments(rm, excitation_type: str, dmm_addr: Optional[str],
                         src_addr: Optional[str], source_cfg_dir: Path,
                         source_label: str, log: LogFn, dmm_cfg_dir: Path):
    """
    Возвращает (dmm_addr, dmm_cfg, src_addr, src_cfg).

    Если оба адреса заданы вручную — опрашивает *IDN? по каждому, чтобы
    подобрать json-конфиг. Иначе запускает полное автообнаружение.

    dmm_cfg_dir — каталог конфигов мультиметра в НУЖНОЙ роли: multimeters_current/
    (амперметр) или multimeters_voltage/ (вольтметр) — выбирается вызывающей
    стороной по output_type (ось А-1, PLAN_V2.md), а не жёстко ammeter-каталогом,
    как было раньше (тогда вольтметровые конфиги, добавленные ещё в Ф1, не
    участвовали в автообнаружении вовсе).
    """
    if dmm_addr and src_addr:
        log("Открываю приборы по заданным адресам, определяю модели по *IDN?...")

        dmm_instr = rm.open_resource(dmm_addr)
        dmm_instr.encoding = 'utf-8'
        dmm_idn = dmm_instr.query('*IDN?').strip()
        dmm_instr.close()
        dmm_cfg = find_config_for_idn(dmm_idn, dmm_cfg_dir)
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
        dmm_cfg_dir, source_cfg_dir, rm=rm, source_label=source_label,
    )


def _log_calibration_warnings(instrument_configs, log: LogFn) -> None:
    """
    Уведомление при подключении прибора (п. 3). Молчим только если поверка
    в порядке (статус OK) — во всех остальных случаях оператор должен
    узнать об этом сразу, а не долистывать до шапки CSV после измерения:

      - UNKNOWN — в конфиге нет даты поверки вовсе. Сейчас так для всех
        приборов в репозитории: реальные даты не выдуманы, их должен
        внести оператор из подлинных свидетельств (см. calibration.py) —
        и до тех пор "нет данных" само по себе стоит проговорить, а не
        молча пропускать;
      - DUE_SOON/OVERDUE — громко, с ⚠.

    Не блокирует измерение ни при каком статусе — это решение оператора
    (доверять ли результату как метрологически точному), а не то, что
    программа вправе принять за него.
    """
    for cfg in instrument_configs:
        info = check_calibration(cfg)
        if info.status == CalibrationStatus.UNKNOWN:
            log(f"ℹ {info.message}")
        elif info.status in (CalibrationStatus.DUE_SOON, CalibrationStatus.OVERDUE):
            log(f"⚠ {info.message}")


def write_results_csv(csv_path: Path, df: pd.DataFrame, params: dict,
                      excitation_type: str, unit: str,
                      aborted_reason: Optional[str] = None,
                      instrument_configs: Optional[list] = None) -> None:
    """
    Пишет CSV с шапкой метаданных (# ...) и данными измерения.

    instrument_configs — конфиги использованных приборов (dmm.config,
    src.config), по одному на прибор; для каждого пишется модель и, если в
    конфиге есть дата поверки, её статус (см. calibration.py, п. 3). S/N
    сюда пока не попадает — *IDN? прибора в текущем коде опрашивается
    транзитно при автообнаружении и никуда не сохраняется, а формат ответа
    (порядок полей) у каждого вендора свой; вытаскивать из него серийный
    номер надёжно — отдельная небольшая задача, не смешана с этой правкой.
    """
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write(f"# Датчик: {params['label']}\n")
        for cfg in (instrument_configs or []):
            info = check_calibration(cfg)
            if info.status == CalibrationStatus.UNKNOWN:
                f.write(f"# Прибор: {info.model_name} (дата поверки не указана в конфиге)\n")
            else:
                f.write(f"# Прибор: {info.model_name}\n")
                f.write(f"#   поверка: {info.last_date.isoformat()}, "
                        f"следующая: {info.next_date.isoformat()}"
                        f"{' — ПРОСРОЧЕНА' if info.status == CalibrationStatus.OVERDUE else ''}\n")
        f.write(f"# Тип возбуждения: {excitation_type}\n")
        f.write(f"# Единица измерения возбуждения: {unit}\n")
        f.write(f"# Диапазон заданного возбуждения: {params['X_start']}..{params['X_stop']} {unit}, "
                f"шаг {params['X_step']} {unit}\n")
        output_type = params.get('output_type', 'current')
        f.write(f"# Тип выхода датчика: {output_type}\n")
        f.write(f"# Единица измерения выхода: {OUTPUT_UNITS[output_type]}\n")
        if excitation_type == 'current':
            f.write(f"# Ограничение напряжения: {params['V_limit']} В\n")
        if params.get('I_nom'):
            f.write(f"# Номинальный первичный ток: {params['I_nom']} А\n")
        if params.get('ratio'):
            f.write(f"# Коэффициент преобразования 1:{params['ratio']}\n")
        if params.get('turns') and params['turns'] != 1.0 and excitation_type == 'current':
            f.write(f"# Число витков через датчик: {params['turns']} "
                    f"(реальный вход = X_set × витки, см. колонку X_real)\n")
        branch = params.get('branch', Branch.BOTH.value)
        if branch == Branch.BOTH.value:
            f.write("# Обе полярности сняты автоматически через плату реле; точка X=0 — "
                    "отдельно, без реле (см. колонку Branch)\n")
            preset = params.get('preset', DirectionPreset.DIVERGING.value)
            if preset != DirectionPreset.DIVERGING.value:
                f.write(f"# Схема прохода: {preset}\n")
        else:
            f.write(f"# Снята только одна полярность ({branch}) через плату реле; точка X=0 — "
                    "без источника (см. колонку Branch)\n")
        if params.get('stop_on_error', False):
            f.write(f"# Остановка при превышении погрешности: {params.get('error_threshold', 1.0)}%\n")
        if aborted_reason:
            f.write(f"# Измерение прервано досрочно: {aborted_reason}\n")
        f.write(f"# Задержка установки: {params['delay']} с\n")
        f.write(f"# Задержка охлаждения: {params['cooling_delay']} с\n")
        if params.get('adaptive_cooling', False):
            f.write(f"# Адаптивная задержка охлаждения (BETA, растёт с током до ×"
                    f"{params.get('adaptive_cooling_max_multiplier', DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER):.1f} "
                    f"на максимуме развёртки)\n")
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
    on_session_open: Optional[Callable[['SessionHandle'], None]] = None,
) -> pd.DataFrame:
    """
    Полный цикл: подобрать/открыть приборы и реле, снять обе ветви (или одну, если params['branch'] != 'both'),
    записать CSV по пути csv_path. Возвращает DataFrame результатов.

    rm               — уже созданный pyvisa.ResourceManager (см. visa_backend).
    params           — словарь параметров из cli.resolve_measure_params.
    dmm/src/relay    — необязательные ручные адреса (иначе автообнаружение).
    log              — колбэк вывода (по умолчанию print).
    should_stop      — колбэк кооперативной остановки (для GUI).
    on_session_open  — вызывается сразу после открытия приборов и получает
                       SessionHandle. Через него вызывающий код (GUI) может
                       обесточить стенд немедленно, не дожидаясь, пока
                       измерительный цикл дойдёт до проверки should_stop.

    Гарантирует выключение источника и реле в блоке finally, даже при ошибке.
    """
    excitation_type = params['excitation_type']
    unit = EXCITATION_UNITS[excitation_type]
    output_type = params.get('output_type', 'current')
    source_cfg_dir = current_source_cfg_dir() if excitation_type == 'current' else voltage_source_cfg_dir()
    source_label = "источник тока" if excitation_type == 'current' else "источник напряжения"
    # Ось А-1 (PLAN_V2.md): роль мультиметра — по тому, ЧТО измеряет (выход
    # датчика), а не по тому, чем датчик возбуждают — это два независимых
    # выбора (см. measurement.run_measurement, output_type).
    dmm_cfg_dir = multimeter_cfg_dir() if output_type == 'current' else voltmeter_cfg_dir()

    dmm_addr, dmm_cfg, src_addr, src_cfg = _resolve_instruments(
        rm, excitation_type, dmm_addr, src_addr, source_cfg_dir, source_label, log, dmm_cfg_dir,
    )

    if relay_port:
        log(f"Плата реле: используется заданный порт {relay_port}")
    else:
        relay_port = discover_relay_port()

    # Ручка публикуется по мере открытия приборов, а не одним куском в конце:
    # если инициализация упадёт на середине (например, реле не отвечает),
    # уже открытый источник всё равно должен быть доступен для аварийного
    # обесточивания.
    handle = SessionHandle()
    if on_session_open is not None:
        on_session_open(handle)

    dmm = handle.dmm = Multimeter(dmm_addr, dmm_cfg, rm=rm)
    src = handle.src = (CurrentSource(src_addr, src_cfg, rm=rm)
                        if excitation_type == 'current'
                        else VoltageSource(src_addr, src_cfg, rm=rm))
    relay = handle.relay = RelayController(relay_port)

    log("Приборы и реле инициализированы. Начинаю измерения...")
    _log_calibration_warnings([dmm.config, src.config], log)

    aborted_reason = None
    # Накопитель точек живёт снаружи вызова: аварийный останов обесточивает
    # стенд из другого потока и закрывает сессии, после чего цикл падает,
    # не успев ничего вернуть. Точки, снятые до нажатия «Стоп», от этого
    # теряться не должны — как раз они обычно и объясняют, почему оператор
    # нажал «Стоп».
    results = []
    try:
        run_measurement(
            dmm, src, relay, excitation_type,
            X_start=params['X_start'], X_stop=params['X_stop'], X_step=params['X_step'],
            V_limit=params['V_limit'], delay=params['delay'], cooling_delay=params['cooling_delay'],
            output_type=output_type,
            branch=Branch(params.get('branch', Branch.BOTH.value)),
            preset=DirectionPreset(params.get('preset', DirectionPreset.DIVERGING.value)),
            turns=params.get('turns') or 1.0,
            averaging_count=params.get('averaging_count', DEFAULT_AVERAGING_COUNT),
            averaging_delay=params.get('averaging_delay', DEFAULT_AVERAGING_DELAY),
            discard_first=params.get('discard_first', DEFAULT_DISCARD_FIRST),
            adaptive_cooling=params.get('adaptive_cooling', False),
            adaptive_cooling_max_multiplier=params.get(
                'adaptive_cooling_max_multiplier', DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER,
            ),
            should_stop=should_stop,
            ratio=params.get('ratio'),
            stop_on_error=params.get('stop_on_error', False),
            error_threshold=params.get('error_threshold', 1.0),
            log_callback=log,
            results_sink=results,
        )
        # Возвращаемый список намеренно игнорируется: его содержимое
        # совпадает с накопителем, а накопитель переживает аварийный останов.
    except Exception:
        # Аварийный останов закрывает сессии из другого потока, поэтому
        # падение цикла здесь — ожидаемое следствие нажатия «Стоп», а не
        # сбой. Всё, что успели снять, лежит в results и будет записано.
        if not handle.stopped:
            raise
    finally:
        # По одному в try: аварийный останов мог уже закрыть часть сессий,
        # и падение на первой не должно оставить остальные открытыми.
        for instrument in (dmm, src, relay):
            try:
                instrument.close()
            except Exception:
                pass

    if handle.stopped:
        log("Измерение прервано аварийным остановом: стенд обесточен.")
    elif aborted_reason:
        log(f"Измерение прервано досрочно: {aborted_reason}")
    else:
        log("Измерения завершены, источник и реле выключены.")

    df = pd.DataFrame(results)
    write_results_csv(csv_path, df, params, excitation_type, unit, aborted_reason,
                      instrument_configs=[dmm.config, src.config])
    log(f"Данные сохранены в {csv_path}")

    return df
