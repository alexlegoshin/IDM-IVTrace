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
    DEFAULT_ADAPTIVE_COOLING_MIN_DELAY, DEFAULT_ADAPTIVE_COOLING_MAX_DELAY,
    MAX_MEASUREMENT_ATTEMPTS,
)
from sweep import Branch, DirectionPreset, plan_custom_sweep
from safety import emergency_shutdown
from calibration import CalibrationStatus, resolve_calibration_info
from applog import get_logger

# Файловый логгер (applog). Назван _flog, а не log, потому что в этом модуле
# `log` — это параметр-колбэк вывода для оператора (LogFn, по умолчанию print);
# путать их нельзя.
_flog = get_logger(__name__)


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
                         source_label: str, log_fn: LogFn, dmm_cfg_dir: Path,
                         relay_port: Optional[str] = None):
    """
    Возвращает (dmm_addr, dmm_cfg, src_addr, src_cfg).

    Если оба адреса заданы вручную — опрашивает *IDN? по каждому, чтобы
    подобрать json-конфиг. Иначе запускает полное автообнаружение.

    relay_port (п.4) — известный порт платы реле (обычно при ручном вводе):
    передаётся в discover_instruments, чтобы её ASRL-ресурс не опрашивался
    `*IDN?` (плата на SCPI не отвечает, открытие её порта — лишний таймаут и
    сброс). При автопоиске порта реле (relay_port is None на этом этапе)
    исключение не применяется — главный конфликт автопоиска (гонка с фоновым
    сервисом) снят синхронным discovery.pause(), см. discovery.py.

    dmm_cfg_dir — каталог конфигов мультиметра в НУЖНОЙ роли: multimeters_current/
    (амперметр) или multimeters_voltage/ (вольтметр) — выбирается вызывающей
    стороной по output_type (ось А-1, PLAN_V2.md), а не жёстко ammeter-каталогом,
    как было раньше (тогда вольтметровые конфиги, добавленные ещё в Ф1, не
    участвовали в автообнаружении вовсе).
    """
    if dmm_addr and src_addr:
        log_fn("Открываю приборы по заданным адресам, определяю модели по *IDN?...")

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
        exclude_relay_port=relay_port,
    )


def _log_calibration_warnings(instrument_configs, log: LogFn) -> None:
    """
    Уведомление при подключении прибора (п. 3). Молчим только если поверка
    в порядке (статус OK) — во всех остальных случаях оператор должен
    узнать об этом сразу, а не долистывать до шапки CSV после измерения:

      - UNKNOWN — прибор не заведён в реестре (instrument_registry.json,
        см. calibration.py) или в его записи нет даты поверки. Реальные
        даты не выдуманы, их должен внести оператор из подлинных
        свидетельств — до тех пор "нет данных" стоит проговорить, а не
        молча пропускать;
      - DUE_SOON/OVERDUE — громко, с ⚠;
      - AMBIGUOUS — в реестре несколько приборов этой модели, по *IDN? не
        различить, какой подключён — тоже громко: оператор должен знать,
        что поверка НЕ проверена автоматически.
    """
    for cfg in instrument_configs:
        info = resolve_calibration_info(cfg)
        if info.status == CalibrationStatus.UNKNOWN:
            log(f"ℹ {info.message}")
            _flog.info("Поверка: %s", info.message)
        elif info.status in (CalibrationStatus.DUE_SOON, CalibrationStatus.OVERDUE, CalibrationStatus.AMBIGUOUS):
            log(f"⚠ {info.message}")
            _flog.warning("Поверка: %s", info.message)


def write_results_csv(csv_path: Path, df: pd.DataFrame, params: dict,
                      excitation_type: str, unit: str,
                      aborted_reason: Optional[str] = None,
                      instrument_configs: Optional[list] = None) -> None:
    """
    Пишет CSV с шапкой метаданных (# ...) и данными измерения.

    instrument_configs — конфиги использованных приборов (dmm.config,
    src.config), по одному на прибор; для каждого пишется модель и статус
    поверки, разрешённый через реестр физических приборов (см.
    calibration.resolve_calibration_info, п. 3/6/7). S/N сюда пока не
    попадает — *IDN? прибора в текущем коде опрашивается транзитно при
    автообнаружении и никуда не сохраняется, а формат ответа (порядок
    полей) у каждого вендора свой; вытаскивать из него серийный номер
    надёжно — отдельная небольшая задача, не смешана с этой правкой. Это же
    ограничение — причина статуса AMBIGUOUS, когда в реестре несколько
    приборов одной модели.
    """
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write(f"# Датчик: {params['label']}\n")
        for cfg in (instrument_configs or []):
            info = resolve_calibration_info(cfg)
            if info.status == CalibrationStatus.UNKNOWN:
                f.write(f"# Прибор: {info.model_name} (прибор не заведён в реестре поверки)\n")
            elif info.status == CalibrationStatus.AMBIGUOUS:
                f.write(f"# Прибор: {info.model_name} "
                        f"(в реестре несколько приборов этой модели — поверка не определена однозначно)\n")
            else:
                f.write(f"# Прибор: {info.model_name}\n")
                f.write(f"#   поверка: {info.last_date.isoformat()}, "
                        f"следующая: {info.next_date.isoformat()}"
                        f"{' — ПРОСРОЧЕНА' if info.status == CalibrationStatus.OVERDUE else ''}\n")
        f.write(f"# Тип возбуждения: {excitation_type}\n")
        f.write(f"# Единица измерения возбуждения: {unit}\n")
        if params.get('custom_program'):
            f.write(f"# Кастомная программа измерения: {params['custom_program']}\n")
        else:
            f.write(f"# Диапазон заданного возбуждения: {params['X_start']}..{params['X_stop']} {unit}, "
                    f"шаг {params['X_step']} {unit}\n")
        output_type = params.get('output_type', 'current')
        f.write(f"# Тип выхода датчика: {output_type}\n")
        f.write(f"# Единица измерения выхода: {OUTPUT_UNITS[output_type]}\n")
        if output_type == 'voltage':
            f.write("# Выход по напряжению — BETA, не проверено на реальном стенде\n")
        if excitation_type == 'current':
            f.write(f"# Ограничение напряжения: {params['V_limit']} В\n")
        else:
            f.write(f"# Ограничение тока: {params.get('I_limit')} А\n")
        if params.get('I_nom'):
            # Баг-репорт: раньше здесь всегда писалось "первичный ток... А",
            # даже при возбуждении напряжением — подпись не имела отношения
            # к тому, что I_nom реально означает в этом случае.
            if excitation_type == 'current':
                f.write(f"# Номинальный первичный ток: {params['I_nom']} А\n")
            else:
                f.write(f"# Номинальное первичное напряжение: {params['I_nom']} В\n")
        if params.get('ratio'):
            f.write(f"# Коэффициент преобразования 1:{params['ratio']}\n")
        if params.get('I_nom') and params.get('ratio'):
            f.write(
                "# Погрешность — приведённая, по ГОСТ 8.401-80 (формула 3, п.2.3.5): "
                "γ = ΔY / Y_N × 100%, где Y_N — номинальный выходной сигнал датчика "
                "(I_nom × витки / коэффициент преобразования), нормирующее значение "
                "принято равным номинальному\n"
            )
        if params.get('zero_offset'):
            f.write(f"# Смещение нуля: {params['zero_offset']} {OUTPUT_UNITS[output_type]} "
                    f"(вычитается из Y_meas при расчёте погрешности — см. analysis.py)\n")
        if params.get('turns') and params['turns'] != 1.0 and excitation_type == 'current':
            f.write(f"# Число витков через датчик: {params['turns']} "
                    f"(реальный вход = X_set × витки, см. колонку X_real)\n")
        if params.get('custom_program'):
            # branch/preset не участвуют в кастомной программе вовсе (см.
            # sweep.plan_custom_sweep) — полярность каждой точки видна
            # буквально в колонке Branch, отдельного пояснения не требует.
            pass
        else:
            branch = params.get('branch', Branch.BOTH.value)
            if branch == Branch.BOTH.value:
                f.write("# Обе полярности сняты автоматически через плату реле; точка X=0 — "
                        "отдельно, без реле (см. колонку Branch)\n")
                preset = params.get('preset', DirectionPreset.DIVERGING.value)
                if preset != DirectionPreset.DIVERGING.value:
                    f.write(f"# Схема прохода: {preset}\n")
                if preset == DirectionPreset.FULL_CYCLE.value and params.get('zero_crossing_smooth'):
                    f.write("# Плавный проход нуля: у нуля ток менялся мелким подшагом "
                            "(п.18), без резкого скачка\n")
            elif branch == Branch.NO_RELAY.value:
                f.write("# Снята одна полярность без платы реле (режим \"No Relay\") — "
                        "коммутация не использовалась вовсе (см. колонку Branch)\n")
            else:
                f.write(f"# Снята только одна полярность ({branch}) через плату реле; точка X=0 — "
                        "без источника (см. колонку Branch)\n")
        if params.get('stop_on_error', False):
            f.write(f"# Остановка при превышении погрешности: {params.get('error_threshold', 1.0)}%\n")
        if aborted_reason:
            f.write(f"# Измерение прервано досрочно: {aborted_reason}\n")
        if params.get('smooth_ramp', False):
            f.write(f"# Плавное нарастание тока (BETA): время перехода между точками "
                    f"{params.get('ramp_duration') or 1.0} с (delay/cooling_delay не применялись)\n")
        else:
            f.write(f"# Задержка установки: {params['delay']} с\n")
            if params.get('adaptive_cooling', False):
                min_delay = params.get('adaptive_cooling_min_delay', DEFAULT_ADAPTIVE_COOLING_MIN_DELAY)
                max_delay = params.get('adaptive_cooling_max_delay', DEFAULT_ADAPTIVE_COOLING_MAX_DELAY)
                f.write(f"# Адаптивная задержка охлаждения (BETA, растёт с током): "
                        f"{min_delay} с на нуле … {max_delay} с на максимуме развёртки\n")
            else:
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
    on_session_open: Optional[Callable[['SessionHandle'], None]] = None,
    on_point_done: Optional[Callable[[int, int], None]] = None,
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

    # relay_port на этом этапе — только ЗАДАННЫЙ вручную (автопоиск порта реле
    # ниже); передаём его, чтобы discover_instruments не слал `*IDN?` в плату.
    dmm_addr, dmm_cfg, src_addr, src_cfg = _resolve_instruments(
        rm, excitation_type, dmm_addr, src_addr, source_cfg_dir, source_label, log, dmm_cfg_dir,
        relay_port=relay_port,
    )

    branch = Branch(params.get('branch', Branch.BOTH.value))
    no_relay = (branch == Branch.NO_RELAY)

    if no_relay:
        # Режим "без реле" (feature "No Relay"): стенд физически без платы
        # коммутации — не открываем порт и не ищем его вовсе, а не просто
        # "не используем то, что нашли". sweep.plan_sweep() в этом режиме
        # форсирует relay=None на каждой точке (см. sweep.py), поэтому
        # measurement.run_measurement() безопасно принимает relay=None.
        log("Реле не используется (режим \"No Relay\").")
    elif relay_port:
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

    _flog.info("Открываю приборы: мультиметр %s (%s), источник %s (%s), реле %s",
               dmm_addr, dmm_cfg.stem, src_addr, src_cfg.stem,
               "нет (No Relay)" if no_relay else relay_port)
    dmm = handle.dmm = Multimeter(dmm_addr, dmm_cfg, rm=rm)
    src = handle.src = (CurrentSource(src_addr, src_cfg, rm=rm)
                        if excitation_type == 'current'
                        else VoltageSource(src_addr, src_cfg, rm=rm))
    relay = handle.relay = None if no_relay else RelayController(relay_port)

    log("Приборы и реле инициализированы. Начинаю измерения...")
    _flog.info("Приборы и реле инициализированы")
    suppress_notifications = params.get('suppress_notifications', False)
    # п.38: галочка "отключить все предупреждения" гасит и уведомления о
    # поверке — но не саму проверку (см. calibration.py — она не блокирует
    # измерение ни при каком статусе, значит нечего и блокировать здесь).
    if not suppress_notifications:
        _log_calibration_warnings([dmm.config, src.config], log)

    aborted_reason = None
    # Накопитель точек живёт снаружи вызова: аварийный останов обесточивает
    # стенд из другого потока и закрывает сессии, после чего цикл падает,
    # не успев ничего вернуть. Точки, снятые до нажатия «Стоп», от этого
    # теряться не должны — как раз они обычно и объясняют, почему оператор
    # нажал «Стоп».
    results = []
    # Планировщик кастомных программ (feature): при заданном custom_program
    # X_start/X_stop/X_step/branch/preset не имеют смысла вовсе — план
    # строится буквально из текста программы (см. sweep.plan_custom_sweep),
    # run_measurement() принимает его через plan_override и не трогает
    # эти четыре параметра (см. measurement.run_measurement, docstring).
    custom_program = params.get('custom_program')
    plan_override = plan_custom_sweep(custom_program) if custom_program else None
    # Число перепромеров при подозрении на брак (баг-репорт п.12): в params —
    # recheck_count (число ДОПОЛНИТЕЛЬНЫХ промеров, 0 = без перепромеров);
    # max_attempts = 1 + recheck_count. Без ключа — прежнее поведение (дефолт).
    recheck_count = params.get('recheck_count', MAX_MEASUREMENT_ATTEMPTS - 1)
    max_attempts = max(1, 1 + int(recheck_count))
    _flog.info("Старт измерения: датчик=%r, возбуждение=%s, выход=%s, branch=%s, попыток на точку=%d",
               params.get('label'), excitation_type, output_type, branch.value, max_attempts)
    try:
        _, run_aborted = run_measurement(
            dmm, src, relay, excitation_type,
            X_start=params.get('X_start') or 0.0, X_stop=params.get('X_stop') or 0.0,
            X_step=params.get('X_step') or 1.0,
            V_limit=params['V_limit'], I_limit=params.get('I_limit'),
            delay=params['delay'], cooling_delay=params['cooling_delay'],
            output_type=output_type,
            branch=branch,
            preset=DirectionPreset(params.get('preset', DirectionPreset.DIVERGING.value)),
            turns=params.get('turns') or 1.0,
            zero_offset=params.get('zero_offset') or 0.0,
            averaging_count=params.get('averaging_count', DEFAULT_AVERAGING_COUNT),
            averaging_delay=params.get('averaging_delay', DEFAULT_AVERAGING_DELAY),
            discard_first=params.get('discard_first', DEFAULT_DISCARD_FIRST),
            adaptive_cooling=params.get('adaptive_cooling', False),
            adaptive_cooling_min_delay=params.get(
                'adaptive_cooling_min_delay', DEFAULT_ADAPTIVE_COOLING_MIN_DELAY,
            ),
            adaptive_cooling_max_delay=params.get(
                'adaptive_cooling_max_delay', DEFAULT_ADAPTIVE_COOLING_MAX_DELAY,
            ),
            should_stop=should_stop,
            ratio=params.get('ratio'),
            I_nom=params.get('I_nom'),
            stop_on_error=params.get('stop_on_error', False),
            error_threshold=params.get('error_threshold', 1.0),
            log_callback=log,
            results_sink=results,
            suppress_notifications=suppress_notifications,
            smooth_ramp=params.get('smooth_ramp', False),
            ramp_duration=params.get('ramp_duration') or 1.0,
            plan_override=plan_override,
            max_attempts=max_attempts,
            on_point_done=on_point_done,
            zero_crossing_smooth=params.get('zero_crossing_smooth', False),
        )
        # Точки берём из накопителя (results_sink) — он переживает аварийный
        # останов; а вот aborted_reason возвращается ТОЛЬКО из штатного выхода
        # run_measurement (досрочная остановка по погрешности, stop_on_error).
        # Баг-репорт: раньше возврат игнорировался целиком, aborted_reason
        # оставался None, и причина досрочной остановки не попадала ни в шапку
        # CSV, ни в финальный лог (ветка elif была мёртвой).
        if run_aborted:
            aborted_reason = run_aborted
    except Exception:
        # Аварийный останов закрывает сессии из другого потока, поэтому
        # падение цикла здесь — ожидаемое следствие нажатия «Стоп», а не
        # сбой. Всё, что успели снять, лежит в results и будет записано.
        if not handle.stopped:
            _flog.exception("Измерение упало (не аварийный останов)")
            raise
    finally:
        # По одному в try: аварийный останов мог уже закрыть часть сессий,
        # и падение на первой не должно оставить остальные открытыми.
        # relay может быть None в режиме "No Relay" — там нет сессии для закрытия.
        # Перед закрытием — возврат SCPI-приборов в местное управление (п.8),
        # чтобы после цикла оператор мог пользоваться передней панелью, не
        # передёргивая питание. Только dmm/src (у реле нет SCPI-режима REMOTE);
        # best-effort, аварийный останов мог уже закрыть сессию.
        restore_local = params.get('restore_local', True)
        for instrument in (dmm, src, relay):
            if instrument is None:
                continue
            if restore_local and instrument is not relay:
                try:
                    instrument.go_local()
                except Exception:
                    pass
            try:
                instrument.close()
            except Exception:
                pass

    if handle.stopped:
        log("Измерение прервано аварийным остановом: стенд обесточен.")
        _flog.warning("Измерение прервано аварийным остановом")
    elif aborted_reason:
        log(f"Измерение прервано досрочно: {aborted_reason}")
        _flog.warning("Измерение прервано досрочно: %s", aborted_reason)
    else:
        log("Измерения завершены, источник и реле выключены.")

    df = pd.DataFrame(results)
    write_results_csv(csv_path, df, params, excitation_type, unit, aborted_reason,
                      instrument_configs=[dmm.config, src.config])
    log(f"Данные сохранены в {csv_path}")
    _flog.info("Измерение завершено: точек %d, файл %s", len(df), csv_path)

    return df


class ManualControlSession:
    """
    Ручное управление стендом вне измерительного цикла (Ф4, п.13 — прямое
    управление реле, п.40 — прямая знаковая уставка).

    В отличие от run_measurement_session() (открыть -> прогнать развёртку ->
    закрыть за один вызов), эта сессия держит приборы открытыми между
    вызовами: оператор нажимает кнопки одну за другой, а не запускает
    развёртку. Приборы закрываются явно через close().

    apply_setpoint() использует ту же логику "знак -> реле", что и
    планировщик (sweep.py, Ф2) — value>0 включает форвард, value<0 —
    реверс, value==0 всё выключает (симметрично точке X=0 в измерительном
    цикле, см. measurement._measure_zero_row: реле там тоже не трогается
    ради нулевого сигнала). Но это именно ОДНА удерживаемая точка, не
    развёртка — переключение реле НЕ ждёт, что кто-то опишет план.

    Аварийный останов — SessionHandle.emergency_stop(), тот же путь, что и
    у измерения: одна и та же логика безопасности для обоих режимов, не
    две параллельных и потенциально рассинхронизированных.
    """

    def __init__(self, handle: 'SessionHandle', excitation_type: str, log: LogFn = print):
        self.handle = handle
        self.excitation_type = excitation_type
        self.log = log
        self._relay_state: Optional[str] = None

    def set_relay(self, direction: str) -> str:
        """direction: 'forward' | 'reverse' | 'off'. Возвращает ответ платы (см. relay.RelayController)."""
        if direction == 'forward':
            resp = self.handle.relay.forward()
        elif direction == 'reverse':
            resp = self.handle.relay.reverse()
        elif direction == 'off':
            resp = self.handle.relay.off()
        else:
            raise ValueError(f"Неизвестное положение реле: {direction!r} (ожидается forward/reverse/off)")
        self._relay_state = direction if direction != 'off' else None
        self.log(f"Реле: {direction} -> {resp}")
        return resp

    def apply_setpoint(self, value: float) -> None:
        """
        Прямая знаковая уставка (п.40). Проверка пределов реле (п.28,
        limits.relay_current_block_reason/relay_current_warning) — забота
        вызывающей стороны (GUI/CLI) ДО этого вызова: сюда сознательно не
        встроена, чтобы не дублировать ту же проверку, что уже есть для
        обычной развёртки, второй, отдельно поддерживаемой копией.
        """
        if value == 0:
            self.stop()
            return
        direction = 'forward' if value > 0 else 'reverse'
        if direction != self._relay_state:
            self.set_relay(direction)
        magnitude = abs(value)
        if self.excitation_type == 'current':
            self.handle.src.set_current(magnitude)
        else:
            self.handle.src.set_voltage(magnitude)
        self.handle.src.output_on()
        self.log(f"Уставка: {value:+g} ({direction})")

    def read(self) -> Optional[float]:
        """Текущее показание мультиметра, если он открыт в этой сессии; иначе None."""
        if self.handle.dmm is None:
            return None
        try:
            return self.handle.dmm.measure()
        except Exception:
            return None

    def stop(self) -> None:
        """Штатное (не аварийное) выключение: источник в 0 -> выход выключен -> реле разомкнуто."""
        try:
            self.handle.src.shutdown()
        except Exception:
            pass
        try:
            self.handle.relay.off()
        except Exception:
            pass
        self._relay_state = None
        self.log("Ручной режим: остановлено (источник выключен, реле разомкнуто).")

    def emergency_stop(self, log: Optional[LogFn] = None):
        return self.handle.emergency_stop(log)

    def close(self) -> None:
        # Возврат SCPI-приборов в местное управление (п.8) перед закрытием —
        # чтобы после ручного режима передняя панель снова работала; у реле
        # SCPI-режима REMOTE нет, его пропускаем.
        for instrument in (self.handle.dmm, self.handle.src):
            if instrument is not None:
                try:
                    instrument.go_local()
                except Exception:
                    pass
        for instrument in (self.handle.dmm, self.handle.src, self.handle.relay):
            if instrument is not None:
                try:
                    instrument.close()
                except Exception:
                    pass


def open_manual_control_session(
    rm,
    excitation_type: str,
    V_limit: Optional[float] = None,
    I_limit: Optional[float] = None,
    dmm_addr: Optional[str] = None,
    src_addr: Optional[str] = None,
    relay_port: Optional[str] = None,
    log: LogFn = print,
    on_session_open: Optional[Callable[['SessionHandle'], None]] = None,
) -> ManualControlSession:
    """
    Открывает приборы для ручного режима (п.13/40) и возвращает
    ManualControlSession, готовую к set_relay()/apply_setpoint().

    Использует то же обнаружение приборов, что и run_measurement_session
    (см. _resolve_instruments/discover_relay_port) — включая мультиметр:
    он в ручном режиме не обязателен для самих команд, но даёт
    ManualControlSession.read() живое показание, а открытие уже
    доказанным путём безопаснее собственного, отдельно поддерживаемого.

    V_limit — ограничение напряжения источника тока (compliance voltage);
    обязателен при excitation_type='current' по той же причине, что и в
    обычной развёртке (run_measurement) — без него источник может попытаться
    поднять напряжение сколь угодно высоко, добиваясь заданного тока. Для
    excitation_type='voltage' не используется (см. VoltageSource.setup).

    I_limit — симметричное ограничение тока источника напряжения (защита от
    короткого замыкания/низкоомной нагрузки на выходе); обязателен при
    excitation_type='voltage'. Для excitation_type='current' не используется.

    Приборы остаются открытыми — закрыть их обязан вызывающий код через
    ManualControlSession.close() (или аварийным остановом).
    """
    if excitation_type == 'current' and V_limit is None:
        raise ValueError("Для возбуждения током необходимо указать ограничение напряжения источника (V_limit).")
    if excitation_type == 'voltage' and I_limit is None:
        raise ValueError("Для возбуждения напряжением необходимо указать ограничение тока источника (I_limit).")

    source_cfg_dir = current_source_cfg_dir() if excitation_type == 'current' else voltage_source_cfg_dir()
    source_label = "источник тока" if excitation_type == 'current' else "источник напряжения"
    dmm_cfg_dir = multimeter_cfg_dir()

    dmm_addr, dmm_cfg, src_addr, src_cfg = _resolve_instruments(
        rm, excitation_type, dmm_addr, src_addr, source_cfg_dir, source_label, log, dmm_cfg_dir,
        relay_port=relay_port,
    )

    if relay_port:
        log(f"Плата реле: используется заданный порт {relay_port}")
    else:
        relay_port = discover_relay_port()

    handle = SessionHandle()
    if on_session_open is not None:
        on_session_open(handle)

    handle.dmm = Multimeter(dmm_addr, dmm_cfg, rm=rm)
    handle.src = (CurrentSource(src_addr, src_cfg, rm=rm)
                 if excitation_type == 'current'
                 else VoltageSource(src_addr, src_cfg, rm=rm))
    handle.relay = RelayController(relay_port)

    if excitation_type == 'current':
        handle.src.setup(voltage_limit=V_limit)
    else:
        handle.src.setup(voltage_limit=0.0, current_limit=I_limit)
    log("Ручной режим: приборы и реле готовы.")
    _log_calibration_warnings([handle.dmm.config, handle.src.config], log)

    return ManualControlSession(handle, excitation_type, log=log)
