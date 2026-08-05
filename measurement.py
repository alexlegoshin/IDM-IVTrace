import math
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple, Union

import pyvisa

from instruments import CurrentSource, VoltageSource
from instruments import Multimeter as DMM
from instruments import is_overflow_reading
from relay import RelayController
from sweep import Branch, DirectionPreset, SweepPoint, plan_sweep

# Единицы измерения задаваемой величины возбуждения — используются и в
# именах колонок CSV, и в подписях графиков analysis.py.
EXCITATION_UNITS = {
    'current': 'A',
    'voltage': 'V',
}

# п.29: усреднение по умолчанию — 4 отсчёта, без задержки между ними,
# первый отбрасывается (защита от случая, когда авто-диапазон ещё не
# устаканился к первому чтению).
DEFAULT_AVERAGING_COUNT = 4
DEFAULT_AVERAGING_DELAY = 0.0
DEFAULT_DISCARD_FIRST = True

# п.9: сколько раз в сумме пытаемся снять точку, если она выходит за порог
# погрешности, прежде чем забраковать её окончательно.
MAX_MEASUREMENT_ATTEMPTS = 3

# п.27: во сколько раз задержка охлаждения на самой большой точке развёртки
# может вырасти относительно заданной cooling_delay при adaptive_cooling=True.
DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER = 5.0


def _adaptive_cooling_delay(base_delay: float, magnitude: float, max_magnitude: float,
                             max_multiplier: float = DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER) -> float:
    """
    Задержка охлаждения, растущая с током (п.27, BETA).

    Джоулево тепло ∝ I², поэтому масштаб — квадратичный: на самой большой
    по модулю точке развёртки (magnitude == max_magnitude) задержка
    достигает base_delay * max_multiplier, на нулевой — остаётся
    base_delay (никакой лишней задержки для мелких точек). Между ними —
    квадратичная интерполяция, потолок задан явно (max_multiplier), чтобы
    не мог случайно вырасти без ограничения.

    Алгоритм эмпирический, не подтверждён на реальном стенде — отсюда
    статус BETA (см. PLAN_V2.md п.27): включается отдельной галочкой,
    по умолчанию выключен, обычная фиксированная cooling_delay остаётся
    поведением по умолчанию.
    """
    if max_magnitude <= 0:
        return base_delay
    fraction = min(1.0, magnitude / max_magnitude) ** 2
    return base_delay * (1.0 + fraction * (max_multiplier - 1.0))


def _log(message: str, log_callback: Optional[Callable[[str], None]]) -> None:
    """Вывод хода измерения: в GUI — через колбэк, в CLI — в stdout."""
    if log_callback is not None:
        log_callback(message)
    else:
        print(message)


def _read_attempts(dmm: DMM, count: int, delay: float) -> Tuple[List[float], bool]:
    """
    Один заход из `count` попыток чтения с паузой `delay` между ними.
    Возвращает (валидные показания, было ли хоть одно переполнение).

    Сбой связи (VisaIOError) — если диапазон ещё не на максимуме, поднимаем
    на одну ступень и пробуем прочитать ещё раз, не выходя за пределы этой
    попытки.

    Переполнение (is_overflow_reading, см. instruments.py) — SCPI-сентинел
    вроде ~9.9e37 вместо ошибки, когда величина вышла за пределы текущего
    диапазона (issue #3). Такое показание НЕ идёт в среднее — диапазон
    прыгает сразу на максимум (по факту сентинела невозможно понять,
    насколько именно ушли за предел).
    """
    valid: List[float] = []
    overflowed = False
    for i in range(count):
        if i > 0 and delay > 0:
            time.sleep(delay)
        try:
            v = dmm.measure_current()
        except pyvisa.errors.VisaIOError:
            if dmm.current_range_idx < len(dmm.ranges) - 1:
                dmm.current_range_idx += 1
                dmm.set_range(dmm.ranges[dmm.current_range_idx])
                try:
                    v = dmm.measure_current()
                except Exception:
                    continue
            else:
                continue
        except Exception:
            continue

        if is_overflow_reading(v):
            overflowed = True
            if dmm.current_range_idx < len(dmm.ranges) - 1:
                dmm.current_range_idx = len(dmm.ranges) - 1
                dmm.set_range(dmm.ranges[dmm.current_range_idx])
            continue

        valid.append(v)
    return valid, overflowed


def _read_averaged(dmm: DMM, count: int = DEFAULT_AVERAGING_COUNT,
                    delay: float = DEFAULT_AVERAGING_DELAY,
                    discard_first: bool = DEFAULT_DISCARD_FIRST) -> List[float]:
    """
    Снимает усреднённое показание для одной точки (п.29 — число отсчётов,
    задержка между ними и отбрасывание первого настраиваются).

    Если весь первый заход ушёл в переполнение (валидных показаний нет, но
    хотя бы одно было отброшено как сентинел), диапазон уже поднят на
    максимум внутри _read_attempts — даём один повторный заход тем же
    числом попыток на исправленном диапазоне, а не сразу сдаёмся в NaN.

    discard_first отбрасывает первый ВАЛИДНЫЙ отсчёт (не первую попытку
    вообще — попытки, съеденные переполнением/сбоем связи, и так не входят
    в валидные) — защита от случая, когда предыдущая точка сменила
    диапазон и первое показание на новом диапазоне ещё не устаканилось.
    """
    valid, overflowed = _read_attempts(dmm, count, delay)
    if not valid and overflowed:
        valid, _ = _read_attempts(dmm, count, delay)
    if discard_first and len(valid) > 1:
        valid = valid[1:]
    return valid


def _average(readings: List[float]) -> float:
    return sum(readings) / len(readings) if readings else math.nan


def _measure_zero_row(dmm: DMM, src: Union[CurrentSource, VoltageSource],
                       excitation_type: str, averaging: dict,
                       log_callback: Optional[Callable[[str], None]]) -> Dict:
    """
    Точка X=0: возбуждения нет, поэтому нет смысла ни включать выход
    источника, ни коммутировать реле — полярность физически неразличима
    при нулевом сигнале (см. run_measurement — реле для этой точки не
    трогается вообще, не только здесь).

    Диапазон принудительно ставится на максимум перед чтением: перед самой
    первой точкой плана ещё неизвестно, что покажет прибор, а после смены
    полярности предыдущая ветвь могла оставить диапазон каким угодно —
    безопасный старт, дальше auto_range() сам сузит его по факту показания.
    """
    src.output_off()  # на всякий случай — вдруг остался включён с прошлого раза
    dmm.current_range_idx = len(dmm.ranges) - 1
    dmm.set_range(dmm.ranges[dmm.current_range_idx])

    readings = _read_averaged(dmm, **averaging)
    i_avg = _average(readings)
    if readings:
        dmm.auto_range(i_avg, is_first=True)

    unit = EXCITATION_UNITS[excitation_type]
    _log(f"  [zero] X_уст = +0.0000 {unit}  ->  I_изм = {i_avg:.6f} А (без источника и реле)",
         log_callback)

    return {
        'Timestamp': datetime.now().isoformat(),
        'Branch': 'zero',
        'X_set': 0.0,
        'X_real': 0.0,
        'I_meas_A': i_avg,
        'Rejected': False,
        'RejectReason': '',
        'PolarityMismatch': False,
    }


def _measure_point_row(dmm: DMM, src: Union[CurrentSource, VoltageSource],
                        excitation_type: str, point: SweepPoint,
                        delay: float, cooling_delay: float,
                        ratio: Optional[float], turns: float,
                        averaging: dict,
                        stop_on_error: bool, error_threshold: float,
                        is_first_of_run: bool,
                        log_callback: Optional[Callable[[str], None]],
                        adaptive_cooling: bool = False,
                        max_magnitude: float = 0.0,
                        adaptive_cooling_max_multiplier: float = DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER,
                        ) -> Tuple[Dict, Optional[str]]:
    """
    Измеряет одну ненулевую точку плана — с поправкой на витки (п.37),
    контрольными повторами при отклонении (п.9), отсечкой по погрешности
    после них (п.7) и детектом перепутанной полярности (п.14).

    Реальный вход датчика — point.magnitude * turns (витки умножают
    ампервитки внутри датчика, а не ток в проводе — см. limits.py и
    PLAN_V2.md п.37). Ожидаемый выход датчика считается от РЕАЛЬНОГО входа:
    expected = point.magnitude * turns / ratio.

    До MAX_MEASUREMENT_ATTEMPTS попыток: если очередное усреднённое
    показание выходит за error_threshold, точка перемеряется заново —
    целиком, включая новый заход всех отсчётов усреднения, — а не считается
    сразу браком. Только если ВСЕ попытки подряд показали превышение,
    точка помечается Rejected=True (не идёт в дальнейший расчёт, но
    остаётся в сырых данных) и, если включена stop_on_error, весь свип
    останавливается — это и есть "решение об остановке после механики п.9".

    Без ratio (expected не определить) проверки и повторы не работают —
    измерение просто снимается один раз, как раньше.

    Возвращает (row, aborted_reason).
    """
    unit = EXCITATION_UNITS[excitation_type]
    # X_real — знаковая величина (для CSV/графика: "реальный вход датчика с
    # учётом витков и направления"), поэтому считается от X_set, а не от
    # magnitude. Ожидаемый ВЫХОД (expected), наоборот, — величина по модулю
    # (сравнивается с |i_avg| ниже), для неё нужен именно модуль.
    real_input = point.x_set * turns
    expected = (point.magnitude * turns / ratio) if (ratio and ratio > 0) else None

    i_avg = math.nan
    error_percent: Optional[float] = None
    rejected = False
    reject_reason = ''
    polarity_mismatch = False
    aborted_reason: Optional[str] = None

    for attempt in range(1, MAX_MEASUREMENT_ATTEMPTS + 1):
        if excitation_type == 'current':
            src.set_current(point.magnitude)
        else:
            src.set_voltage(point.magnitude)
        src.output_on()
        time.sleep(delay)

        readings = _read_averaged(dmm, **averaging)
        i_avg = _average(readings)
        if readings:
            dmm.auto_range(i_avg, is_first=(is_first_of_run and attempt == 1))

        src.output_off()
        effective_cooling_delay = (
            _adaptive_cooling_delay(cooling_delay, point.magnitude, max_magnitude,
                                     adaptive_cooling_max_multiplier)
            if adaptive_cooling else cooling_delay
        )
        time.sleep(effective_cooling_delay)

        if expected is None or math.isnan(i_avg):
            break  # нечего сверять с ожиданием — принимаем как есть, повторов нет

        # п.14: перепутанная полярность/ориентация датчика — знак показания
        # не совпадает со знаком уставки. На X=0 не распространяется (сюда
        # эта функция для него не вызывается вовсе, см. _measure_zero_row).
        if i_avg != 0 and (i_avg > 0) != (point.x_set > 0):
            polarity_mismatch = True

        error_percent = abs(abs(i_avg) - expected) / expected * 100.0 if expected > 0 else None
        if error_percent is None or error_percent <= error_threshold:
            break  # в допуске — точка принята

        if attempt == MAX_MEASUREMENT_ATTEMPTS:
            rejected = True
            reject_reason = (
                f"погрешность {error_percent:.2f}% > {error_threshold}% "
                f"({MAX_MEASUREMENT_ATTEMPTS} попытки подряд)"
            )
            if stop_on_error:
                aborted_reason = (
                    f"Погрешность {error_percent:.2f}% превысила порог {error_threshold}% "
                    f"на X_уст = {point.x_set:+.4f} {unit} ({MAX_MEASUREMENT_ATTEMPTS} попытки подряд)"
                )
        # иначе — попытка не последняя, продолжаем цикл (контрольный промер, п.9)

    msg = f"  [{'forward' if point.x_set >= 0 else 'reverse'}] X_уст = {point.x_set:+.4f} {unit}  ->  I_изм = {i_avg:.6f} А"
    if rejected:
        msg += f"  [БРАК: {reject_reason}]"
    if polarity_mismatch:
        msg += "  [ВНИМАНИЕ: похоже, перепутана полярность/ориентация датчика]"
    _log(msg, log_callback)

    row = {
        'Timestamp': datetime.now().isoformat(),
        'Branch': 'forward' if point.x_set >= 0 else 'reverse',
        'X_set': point.x_set,
        'X_real': real_input,
        'I_meas_A': i_avg,
        'Rejected': rejected,
        'RejectReason': reject_reason,
        'PolarityMismatch': polarity_mismatch,
    }
    return row, aborted_reason


def run_measurement(dmm: DMM, src: Union[CurrentSource, VoltageSource], relay: RelayController,
                     excitation_type: str,
                     X_start: float, X_stop: float, X_step: float,
                     V_limit: float, delay: float, cooling_delay: float,
                     branch: Branch = Branch.BOTH,
                     preset: DirectionPreset = DirectionPreset.DIVERGING,
                     turns: float = 1.0,
                     averaging_count: int = DEFAULT_AVERAGING_COUNT,
                     averaging_delay: float = DEFAULT_AVERAGING_DELAY,
                     discard_first: bool = DEFAULT_DISCARD_FIRST,
                     adaptive_cooling: bool = False,
                     adaptive_cooling_max_multiplier: float = DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER,
                     should_stop: Optional[Callable[[], bool]] = None,
                     ratio: Optional[float] = None,
                     stop_on_error: bool = False,
                     error_threshold: float = 1.0,
                     log_callback: Optional[Callable[[str], None]] = None,
                     results_sink: Optional[List[Dict]] = None,
                     ) -> Tuple[List[Dict], Optional[str]]:
    """
    Полный цикл измерения амплитудной характеристики датчика: строит план
    (sweep.plan_sweep — вся комбинаторика знаков/направлений/пресетов там,
    см. п.8/17/18/19) и исполняет его точка за точкой, переключая реле
    только когда требуемое положение меняется между соседними точками.

    turns (п.37) — число витков провода через окно датчика. Реальный вход
    датчика = |X_set| × turns; именно от него считается ожидаемый выход
    (ratio) и колонка X_real в результате. Через реле и провод при этом
    течёт |X_set| — НЕ X_set×turns (см. limits.py и docstring plan_sweep) —
    turns здесь не используется ни для чего, кроме этого пересчёта.

    adaptive_cooling (п.27, BETA) — задержка охлаждения между точками
    растёт квадратично с током (джоулево тепло ∝ I²) вместо фиксированной
    cooling_delay, с потолком adaptive_cooling_max_multiplier относительно
    неё на самой большой точке развёртки. Алгоритм эмпирический, не
    проверен на реальном стенде — выключен по умолчанию, включается явно.

    excitation_type: 'current' — на источник тока подаётся уставка тока
                      (V_limit используется как ограничение по напряжению);
                      'voltage' — на источник напряжения подаётся уставка
                      напряжения (V_limit в этом случае не используется для
                      настройки источника, X_stop и есть максимальное
                      напряжение цикла).

    Выход датчика (измеряемая величина) всегда ток — читается мультиметром
    независимо от типа возбуждения.

    should_stop — необязательный колбэк без аргументов; если он возвращает
    True, цикл прерывается между точками. Используется GUI для кнопки
    «Стоп»; при None (по умолчанию, как в CLI) не проверяется.

    results_sink — необязательный внешний список, куда точка кладётся сразу
    после измерения, а не по возвращении из функции. Нужен, чтобы уже
    снятые точки пережили аварийный останов: он обесточивает стенд из
    другого потока и закрывает сессии, после чего цикл падает, не успев
    ничего вернуть.

    Возвращает (results, aborted_reason): aborted_reason — текст причины
    досрочной остановки по погрешности (после MAX_MEASUREMENT_ATTEMPTS
    подряд неудачных попыток, см. _measure_point_row), либо None, если
    свип прошёл до конца или был прерван пользователем.
    """
    if excitation_type == 'current':
        src.setup(voltage_limit=V_limit)
    elif excitation_type == 'voltage':
        src.setup(voltage_limit=X_stop)
    else:
        raise ValueError(f"Неизвестный тип возбуждения: {excitation_type!r} (ожидается 'current' или 'voltage')")

    plan = plan_sweep(X_start, X_stop, X_step, branch=branch, preset=preset)
    averaging = dict(count=averaging_count, delay=averaging_delay, discard_first=discard_first)
    max_magnitude = max((p.magnitude for p in plan), default=0.0)

    results: List[Dict] = []
    aborted_reason: Optional[str] = None
    current_relay_state: Optional[str] = None  # что реально сейчас установлено на плате
    run_started_fresh = True  # первая точка нового (после смены реле) прогона — под is_first в auto_range

    try:
        for point in plan:
            if should_stop is not None and should_stop():
                _log("\nОстановка по запросу пользователя.", log_callback)
                break

            if point.relay != current_relay_state and point.relay is not None:
                if point.relay == 'forward':
                    _log("\nПереключаю реле: прямое направление (IFW)...", log_callback)
                    _log(f"  Ответ реле: {relay.forward()}", log_callback)
                else:
                    _log("\nПереключаю реле: обратное направление (IRW)...", log_callback)
                    _log(f"  Ответ реле: {relay.reverse()}", log_callback)
                current_relay_state = point.relay
                # При смене направления датчик перемагничивается заново,
                # поэтому выбор диапазона вольтметра начинаем заново.
                dmm.current_range_idx = len(dmm.ranges) - 1
                dmm.set_range(dmm.ranges[dmm.current_range_idx])
                run_started_fresh = True

            if point.is_zero:
                row = _measure_zero_row(dmm, src, excitation_type, averaging, log_callback)
            else:
                row, point_aborted = _measure_point_row(
                    dmm, src, excitation_type, point, delay, cooling_delay,
                    ratio, turns, averaging, stop_on_error, error_threshold,
                    is_first_of_run=run_started_fresh, log_callback=log_callback,
                    adaptive_cooling=adaptive_cooling, max_magnitude=max_magnitude,
                    adaptive_cooling_max_multiplier=adaptive_cooling_max_multiplier,
                )
                run_started_fresh = False
                if point_aborted:
                    aborted_reason = point_aborted

            results.append(row)
            if results_sink is not None:
                results_sink.append(row)

            if aborted_reason:
                break
    finally:
        # Штатное завершение. Каждый шаг отдельно: если аварийный останов уже
        # погасил стенд и закрыл сессии (см. safety.emergency_shutdown), эти
        # вызовы упадут на закрытых сессиях — и не должны при этом ни
        # помешать друг другу, ни подменить собой настоящую причину выхода
        # из try.
        try:
            src.shutdown()
        except Exception:
            pass
        if current_relay_state is not None:
            try:
                relay.off()
            except Exception:
                pass

    return results, aborted_reason
