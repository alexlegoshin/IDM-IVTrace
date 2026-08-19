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

# Единицы измерения ВЫХОДА датчика (ось А-1, PLAN_V2.md) — независимая от
# возбуждения величина: датчик тока возбуждают током И измеряют его выход
# как ток, но у датчика, скажем, тока с выходом по напряжению или у
# датчика напряжения с токовым выходом эти два измерения разные. Мультиметр
# как класс (instruments.Multimeter) уже умеет обе роли через конфиг —
# этот словарь только подписывает колонку/график тем, что реально снято.
OUTPUT_UNITS = {
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
# погрешности, прежде чем забраковать её окончательно. Это ДЕФОЛТ; число
# перепромеров настраивается (баг-репорт п.12) — см. параметр max_attempts
# в _measure_point_row/run_measurement (max_attempts = 1 + число доп. промеров;
# 1 = без перепромеров вовсе, брак сразу по первому промеру вне допуска).
MAX_MEASUREMENT_ATTEMPTS = 3

# п.27: границы адаптивной задержки охлаждения (BETA) — оператор задаёт их
# сам явно (мин./макс. в секундах), а не как множитель от одной базовой
# cooling_delay (было раньше, до баг-репорта об удобстве этого UI).
DEFAULT_ADAPTIVE_COOLING_MIN_DELAY = 0.5
DEFAULT_ADAPTIVE_COOLING_MAX_DELAY = 2.5

# Между шагами плавного нарастания (feature, BETA) — не спамим источник частыми командами
# (баг-репорт: "не надо слать тысячи команд"). Если на переход отведено
# меньше этого порога секунд, промежуточных шагов не будет вовсе — один
# прямой переход, источник сглаживает его сам за счёт собственной
# конструкции (slew rate).
SMOOTH_RAMP_MIN_DURATION_FOR_STEPS_S = 2.0


def _adaptive_cooling_delay(min_delay: float, max_delay: float, magnitude: float,
                             max_magnitude: float) -> float:
    """
    Задержка охлаждения, растущая с током (п.27, BETA).

    Джоулево тепло ∝ I², поэтому масштаб — квадратичный: на самой большой
    по модулю точке развёртки (magnitude == max_magnitude) задержка
    достигает max_delay, на нулевой — остаётся min_delay (никакой лишней
    задержки для мелких точек). Между ними — квадратичная интерполяция.

    Границы (min_delay/max_delay) — то, что оператор явно вводит в UI/CLI
    в секундах (баг-репорт: раньше это была одна базовая cooling_delay,
    умноженная на коэффициент, — неудобно оценить итоговое время на глаз;
    теперь обе границы видны и задаются напрямую).

    Алгоритм эмпирический, не подтверждён на реальном стенде — отсюда
    статус BETA (см. PLAN_V2.md п.27): включается отдельной галочкой,
    по умолчанию выключен, обычная фиксированная cooling_delay остаётся
    поведением по умолчанию.
    """
    if max_magnitude <= 0:
        return min_delay
    fraction = min(1.0, magnitude / max_magnitude) ** 2
    return min_delay + fraction * (max_delay - min_delay)


def _ease_in_out(p: float, k: float = 5.0) -> float:
    """
    Сглаживание перехода 0->1 (feature "плавное нарастание", BETA): первая
    половина — экспоненциальный набор, вторая — та же кривая, зеркально
    перевёрнутая, для плавного выхода на цель (как попросил заказчик:
    "сначала экспонента набор, затем экспонента перевёрнутая"). k — крутизна
    (больше — резче старт/финиш при той же общей длительности).
    """
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    scale = 0.5 / (1.0 - math.exp(-k * 0.5))
    if p <= 0.5:
        return scale * (1.0 - math.exp(-k * p))
    return 1.0 - scale * (1.0 - math.exp(-k * (1.0 - p)))


def _ramp_steps(start: float, target: float, duration: float) -> List[Tuple[float, float]]:
    """
    Возвращает [(значение_уставки, пауза_ПОСЛЕ_записи), ...] для плавного
    перехода start->target за duration секунд.

    Баг-репорт (прямое указание заказчика): не спамить источник частыми
    командами — примерно ОДНА команда на секунду перехода, не больше. Если
    duration меньше SMOOTH_RAMP_MIN_DURATION_FOR_STEPS_S — промежуточных
    шагов просто не может быть достаточно, чтобы это имело смысл: один
    прямой переход без выключения выхода, источник сам сглаживает его
    физически за счёт собственной конструкции (slew rate).
    """
    if duration < SMOOTH_RAMP_MIN_DURATION_FOR_STEPS_S or start == target:
        return [(target, 0.0)]
    n = max(2, round(duration))
    dt = duration / n
    steps = []
    for i in range(1, n + 1):
        frac = _ease_in_out(i / n)
        value = start + (target - start) * frac
        steps.append((value, dt))
    return steps


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
            v = dmm.measure()
        except pyvisa.errors.VisaIOError:
            if dmm.current_range_idx < len(dmm.ranges) - 1:
                dmm.current_range_idx += 1
                dmm.set_range(dmm.ranges[dmm.current_range_idx])
                try:
                    v = dmm.measure()
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
                       excitation_type: str, output_type: str, averaging: dict,
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
    output_unit = OUTPUT_UNITS[output_type]
    _log(f"  [zero] X_уст = +0.0000 {unit}  ->  Y_изм = {i_avg:.6f} {output_unit} (без источника и реле)",
         log_callback)

    return {
        'Timestamp': datetime.now().isoformat(),
        'Branch': 'zero',
        'X_set': 0.0,
        'X_real': 0.0,
        'Y_meas': i_avg,
        'Y_unit': output_unit,
        'Rejected': False,
        'RejectReason': '',
        'PolarityMismatch': False,
    }


def _measure_point_row(dmm: DMM, src: Union[CurrentSource, VoltageSource],
                        excitation_type: str, output_type: str, point: SweepPoint,
                        delay: float, cooling_delay: float,
                        ratio: Optional[float], turns: float,
                        averaging: dict,
                        stop_on_error: bool, error_threshold: float,
                        is_first_of_run: bool,
                        log_callback: Optional[Callable[[str], None]],
                        adaptive_cooling: bool = False,
                        max_magnitude: float = 0.0,
                        adaptive_cooling_min_delay: float = DEFAULT_ADAPTIVE_COOLING_MIN_DELAY,
                        adaptive_cooling_max_delay: float = DEFAULT_ADAPTIVE_COOLING_MAX_DELAY,
                        suppress_notifications: bool = False,
                        zero_offset: float = 0.0,
                        y_sec_nom: Optional[float] = None,
                        max_attempts: int = MAX_MEASUREMENT_ATTEMPTS,
                        ) -> Tuple[Dict, Optional[str]]:
    """
    Измеряет одну ненулевую точку плана — с поправкой на витки (п.37),
    контрольными повторами при отклонении (п.9), отсечкой по погрешности
    после них (п.7) и детектом перепутанной полярности (п.14).

    max_attempts (баг-репорт п.12) — сколько раз ВСЕГО пытаемся снять точку
    при выходе за порог, прежде чем забраковать (1 = без перепромеров: брак
    сразу). Раньше было жёстко MAX_MEASUREMENT_ATTEMPTS=3.

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

    y_sec_nom — номинальный выходной сигнал датчика (I_nom * turns / ratio),
    та же величина, что Y_sec_nom в analysis.py (X_N по ГОСТ 8.401-80,
    п.2.3.5 — нормирующее значение, равное номинальному). Если задан,
    error_percent считается ПРИВЕДЁННОЙ погрешностью по ГОСТ 8.401-80
    (формула 3): отклонение / y_sec_nom — тем же способом, что и в итоговом
    отчёте/графике (баг-репорт: раньше здесь всегда была обычная
    относительная погрешность отклонение/expected, которая на малых
    уставках зашкаливает даже у исправного датчика, и не совпадала с тем,
    что показывает график). Без y_sec_nom (I_nom не задан) — старое
    поведение, относительная погрешность, как и раньше.

    Возвращает (row, aborted_reason).
    """
    unit = EXCITATION_UNITS[excitation_type]
    output_unit = OUTPUT_UNITS[output_type]
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

    for attempt in range(1, max_attempts + 1):
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
            _adaptive_cooling_delay(adaptive_cooling_min_delay, adaptive_cooling_max_delay,
                                     point.magnitude, max_magnitude)
            if adaptive_cooling else cooling_delay
        )
        time.sleep(effective_cooling_delay)

        if expected is None or math.isnan(i_avg):
            break  # нечего сверять с ожиданием — принимаем как есть, повторов нет

        # Смещение нуля (feature) вычитается ДО проверок полярности/
        # погрешности — обе завязаны на знак/величину показания, а
        # известный осознанный сдвиг не должен читаться как перепутанная
        # полярность или как реальное расхождение с ожиданием. Y_meas в
        # результате остаётся СЫРЫМ (см. row ниже) — поправка не изменяет
        # данные, только решения, принятые на их основе.
        i_avg_corrected = i_avg - zero_offset

        # п.14: перепутанная полярность/ориентация датчика — знак показания
        # не совпадает со знаком уставки. На X=0 не распространяется (сюда
        # эта функция для него не вызывается вовсе, см. _measure_zero_row).
        if i_avg_corrected != 0 and (i_avg_corrected > 0) != (point.x_set > 0):
            polarity_mismatch = True

        # Приведённая погрешность (к y_sec_nom), если он известен — та же
        # формула, что и в отчёте/графике (analysis.py); иначе — старая
        # относительная (к expected в этой конкретной точке), см. докстринг.
        denom = y_sec_nom if y_sec_nom else expected
        error_percent = abs(abs(i_avg_corrected) - expected) / denom * 100.0 if denom else None
        if error_percent is None or error_percent <= error_threshold:
            break  # в допуске — точка принята

        if attempt == max_attempts:
            attempts_word = "попытка" if max_attempts == 1 else "попытки подряд"
            rejected = True
            reject_reason = (
                f"погрешность {error_percent:.2f}% > {error_threshold}% "
                f"({max_attempts} {attempts_word})"
            )
            if stop_on_error:
                aborted_reason = (
                    f"Погрешность {error_percent:.2f}% превысила порог {error_threshold}% "
                    f"на X_уст = {point.x_set:+.4f} {unit} ({max_attempts} {attempts_word})"
                )
        # иначе — попытка не последняя, продолжаем цикл (контрольный промер, п.9)

    msg = f"  [{'forward' if point.x_set >= 0 else 'reverse'}] X_уст = {point.x_set:+.4f} {unit}  ->  Y_изм = {i_avg:.6f} {output_unit}"
    if rejected:
        msg += f"  [БРАК: {reject_reason}]"
    # suppress_notifications (п.38) гасит только уведомление в логе — сам
    # факт polarity_mismatch всё равно попадает в PolarityMismatch ниже
    # (сырые данные), галочка "отключить предупреждения" про них не решает.
    if polarity_mismatch and not suppress_notifications:
        msg += "  [ВНИМАНИЕ: похоже, перепутана полярность/ориентация датчика]"
    _log(msg, log_callback)

    row = {
        'Timestamp': datetime.now().isoformat(),
        'Branch': 'forward' if point.x_set >= 0 else 'reverse',
        'X_set': point.x_set,
        'X_real': real_input,
        'Y_meas': i_avg,
        'Y_unit': output_unit,
        'Rejected': rejected,
        'RejectReason': reject_reason,
        'PolarityMismatch': polarity_mismatch,
    }
    return row, aborted_reason


def _measure_point_row_ramp(dmm: DMM, src: CurrentSource, output_type: str,
                            point: SweepPoint, prev_magnitude: float, ramp_duration: float,
                            ratio: Optional[float], turns: float, averaging: dict,
                            stop_on_error: bool, error_threshold: float,
                            is_first_of_run: bool,
                            log_callback: Optional[Callable[[str], None]],
                            zero_offset: float = 0.0,
                            suppress_notifications: bool = False,
                            y_sec_nom: Optional[float] = None,
                            max_attempts: int = MAX_MEASUREMENT_ATTEMPTS,
                            ) -> Tuple[Dict, Optional[str]]:
    """
    Вариант _measure_point_row для плавного нарастания (feature, BETA,
    только возбуждение ТОКОМ, см. run_measurement/smooth_ramp). Отличия от
    обычной точки:

      - вместо мгновенного скачка (set_current + delay) — последовательность
        промежуточных уставок по _ramp_steps от prev_magnitude до
        point.magnitude; источник НЕ выключается ни в начале, ни в конце
        (output_on() вызывается один раз, output_off() здесь нет вовсе —
        см. run_measurement, где выход гасится только на границе реле/в
        конце всего измерения);
      - задержка на охлаждение не применяется вообще — по прямому
        требованию заказчика этот режим и охлаждение взаимно исключают
        друг друга (в UI при включении режима поле охлаждения скрывается);
      - контрольные повторы (п.9) НЕ повторяют само нарастание (это и есть
        "не слать лишние команды на источник") — при превышении порога
        погрешности переснимается только ЧТЕНИЕ, уставка уже стоит на
        месте и её незачем трогать заново.

    Возвращает (row, aborted_reason) — та же форма, что и у обычной точки.
    """
    unit = EXCITATION_UNITS['current']
    output_unit = OUTPUT_UNITS[output_type]
    real_input = point.x_set * turns
    expected = (point.magnitude * turns / ratio) if (ratio and ratio > 0) else None

    src.output_on()
    for value, wait in _ramp_steps(prev_magnitude, point.magnitude, ramp_duration):
        src.set_current(value)
        if wait:
            time.sleep(wait)

    i_avg = math.nan
    error_percent: Optional[float] = None
    rejected = False
    reject_reason = ''
    polarity_mismatch = False
    aborted_reason: Optional[str] = None

    for attempt in range(1, max_attempts + 1):
        readings = _read_averaged(dmm, **averaging)
        i_avg = _average(readings)
        if readings:
            dmm.auto_range(i_avg, is_first=(is_first_of_run and attempt == 1))

        if expected is None or math.isnan(i_avg):
            break

        i_avg_corrected = i_avg - zero_offset
        if i_avg_corrected != 0 and (i_avg_corrected > 0) != (point.x_set > 0):
            polarity_mismatch = True

        # Приведённая погрешность (к y_sec_nom), если он известен — см.
        # докстринг _measure_point_row; иначе старая относительная (к expected).
        denom = y_sec_nom if y_sec_nom else expected
        error_percent = abs(abs(i_avg_corrected) - expected) / denom * 100.0 if denom else None
        if error_percent is None or error_percent <= error_threshold:
            break

        if attempt == max_attempts:
            rejected = True
            reject_reason = (
                f"погрешность {error_percent:.2f}% > {error_threshold}% "
                f"({max_attempts} отсчёта(ов) без повторного нарастания — режим BETA)"
            )
            if stop_on_error:
                aborted_reason = (
                    f"Погрешность {error_percent:.2f}% превысила порог {error_threshold}% "
                    f"на X_уст = {point.x_set:+.4f} {unit} ({max_attempts} отсчёта(ов))"
                )
        # иначе — не последняя попытка, переснимаем ЧТЕНИЕ (см. докстринг)

    msg = (f"  [BETA плавно] X_уст = {point.x_set:+.4f} {unit}  ->  Y_изм = {i_avg:.6f} {output_unit}")
    if rejected:
        msg += f"  [БРАК: {reject_reason}]"
    if polarity_mismatch and not suppress_notifications:
        msg += "  [ВНИМАНИЕ: похоже, перепутана полярность/ориентация датчика]"
    _log(msg, log_callback)

    row = {
        'Timestamp': datetime.now().isoformat(),
        'Branch': 'forward' if point.x_set >= 0 else 'reverse',
        'X_set': point.x_set,
        'X_real': real_input,
        'Y_meas': i_avg,
        'Y_unit': output_unit,
        'Rejected': rejected,
        'RejectReason': reject_reason,
        'PolarityMismatch': polarity_mismatch,
    }
    return row, aborted_reason


def run_measurement(dmm: DMM, src: Union[CurrentSource, VoltageSource], relay: Optional[RelayController],
                     excitation_type: str,
                     X_start: float, X_stop: float, X_step: float,
                     V_limit: float, delay: float, cooling_delay: float,
                     I_limit: Optional[float] = None,
                     output_type: str = 'current',
                     branch: Branch = Branch.BOTH,
                     preset: DirectionPreset = DirectionPreset.DIVERGING,
                     turns: float = 1.0,
                     zero_offset: float = 0.0,
                     averaging_count: int = DEFAULT_AVERAGING_COUNT,
                     averaging_delay: float = DEFAULT_AVERAGING_DELAY,
                     discard_first: bool = DEFAULT_DISCARD_FIRST,
                     adaptive_cooling: bool = False,
                     adaptive_cooling_min_delay: float = DEFAULT_ADAPTIVE_COOLING_MIN_DELAY,
                     adaptive_cooling_max_delay: float = DEFAULT_ADAPTIVE_COOLING_MAX_DELAY,
                     should_stop: Optional[Callable[[], bool]] = None,
                     ratio: Optional[float] = None,
                     I_nom: Optional[float] = None,
                     stop_on_error: bool = False,
                     error_threshold: float = 1.0,
                     log_callback: Optional[Callable[[str], None]] = None,
                     results_sink: Optional[List[Dict]] = None,
                     suppress_notifications: bool = False,
                     smooth_ramp: bool = False,
                     ramp_duration: float = 1.0,
                     plan_override: Optional[List[SweepPoint]] = None,
                     max_attempts: int = MAX_MEASUREMENT_ATTEMPTS,
                     on_point_done: Optional[Callable[[int, int], None]] = None,
                     zero_crossing_smooth: bool = False,
                     ) -> Tuple[List[Dict], Optional[str]]:
    """
    Полный цикл измерения амплитудной характеристики датчика: строит план
    (sweep.plan_sweep — вся комбинаторика знаков/направлений/пресетов там,
    см. п.8/17/18/19) и исполняет его точка за точкой, переключая реле
    только когда требуемое положение меняется между соседними точками.

    plan_override (feature "планировщик кастомных программ") — если
    передан, используется буквально ВМЕСТО plan_sweep(X_start, X_stop,
    X_step, branch, preset) — эти четыре параметра тогда просто
    игнорируются планировщиком (см. sweep.plan_custom_sweep). Сам
    измерительный цикл не знает и не обязан знать, откуда план взялся —
    он одинаково "тупо" исполняет любой список SweepPoint (см. докстринг
    sweep.py).

    zero_offset (feature "offset нуля") — известное смещение нуля датчика
    (некоторые датчики осознанно смещены — выдают ненулевой сигнал при
    X=0). Вычитается из КАЖДОГО ненулевого измерения ДО проверки полярности
    и погрешности (см. _measure_point_row) — то есть влияет на живые
    решения о браке/остановке по погрешности, а не только на пост-обработку
    графика (см. analysis.py — там применяется та же поправка из метаданных
    CSV, чтобы график и живые решения не расходились). В CSV пишется СЫРОЕ
    показание (Y_meas, без поправки) — поправка не модифицирует данные, а
    хранится отдельно (см. orchestrate.write_results_csv, "# Смещение нуля").
    Точка X=0 (_measure_zero_row) поправку не получает — там измеряется
    ровно то смещение, о котором идёт речь, вычитать его из самого себя
    было бы не нужно и вводило бы в заблуждение (X=0 должен показывать
    величину смещения как она есть).

    relay — None допустим тогда и только тогда, когда branch=Branch.NO_RELAY
    (стенд без платы реле, п. "No Relay"): sweep.plan_sweep() в этом режиме
    форсирует relay=None на КАЖДОЙ точке плана, поэтому ветка переключения
    реле (ниже) физически не выполняется и relay не разыменовывается. Для
    любого другого branch relay обязателен.

    smooth_ramp/ramp_duration (feature "плавное нарастание", BETA) — только
    для excitation_type='current'. Выше limits.SMOOTH_RAMP_WARN_CURRENT_A —
    предупреждение (не запрет, см. limits.smooth_ramp_warning); ампераж не
    ограничен. Заменяет обычный скачок set_current+delay на
    плавный переход по _ramp_steps (см. _measure_point_row_ramp) —
    источник остаётся включённым непрерывно между точками ОДНОЙ ветви,
    delay/cooling_delay/adaptive_cooling в этом режиме не применяются вовсе
    (взаимно исключают друг друга по прямому требованию заказчика). На
    границе смены полярности выход всё равно гасится ПЕРЕД коммутацией
    реле (переключать реле под током недопустимо) — следующая ветвь
    начинает плавный набор с нуля, не с прошлого значения предыдущей ветви.

    turns (п.37) — число витков провода через окно датчика. Реальный вход
    датчика = |X_set| × turns; именно от него считается ожидаемый выход
    (ratio) и колонка X_real в результате. Через реле и провод при этом
    течёт |X_set| — НЕ X_set×turns (см. limits.py и docstring plan_sweep) —
    turns здесь не используется ни для чего, кроме этого пересчёта.

    I_nom — номинальный первичный ток/напряжение датчика. Вместе с ratio
    задаёт y_sec_nom = I_nom * turns / ratio (см. _measure_point_row) — ту же
    "полную шкалу", относительно которой analysis.py считает приведённую
    погрешность для отчёта. Если I_nom задан (наряду с ratio) — отсечка по
    погрешности (error_threshold, retries, stop_on_error) работает по
    приведённой погрешности, как и отчёт; если нет — по старой, чисто
    относительной (баг-репорт: раньше I_nom тут не участвовал вовсе, и живая
    отсечка всегда была относительной — расходилась с приведённой из отчёта и
    неправдоподобно росла на малых уставках даже у исправного датчика).

    adaptive_cooling (п.27, BETA) — задержка охлаждения между точками
    растёт квадратично с током (джоулево тепло ∝ I²) вместо фиксированной
    cooling_delay: на нулевой точке — adaptive_cooling_min_delay, на самой
    большой по модулю точке развёртки — adaptive_cooling_max_delay, между
    ними квадратичная интерполяция (см. _adaptive_cooling_delay). Обе
    границы оператор вводит сам в секундах (баг-репорт — раньше это была
    одна база × множитель, границы явно не были видны). Алгоритм
    эмпирический, не проверен на реальном стенде — выключен по умолчанию,
    включается явно; в этом режиме cooling_delay сама не используется вовсе.

    excitation_type: 'current' — на источник тока подаётся уставка тока
                      (V_limit используется как ограничение по напряжению);
                      'voltage' — на источник напряжения подаётся уставка
                      напряжения (V_limit в этом случае не используется для
                      настройки источника, X_stop и есть максимальное
                      напряжение цикла; вместо этого используется I_limit —
                      симметричное ограничение тока источника напряжения,
                      обязательно при excitation_type='voltage').

    output_type (ось А-1, PLAN_V2.md, независимая от excitation_type) —
    что физически измеряет мультиметр на выходе датчика: 'current' (по
    умолчанию, как раньше) или 'voltage'. Сам по себе этот параметр не
    переключает прибор ни во что — какую роль (амперметр/вольтметр)
    реально играет `dmm`, определено ЗАРАНЕЕ тем, с каким конфигом он был
    открыт (см. orchestrate._resolve_instruments, выбирает каталог
    multimeters_current/ или multimeters_voltage/ по этому же output_type).
    Здесь он используется только для подписи колонки результата (`Y_unit`)
    и текста в логе — измерительный цикл одинаково читает dmm.measure()
    независимо от того, что именно эта величина означает.

    should_stop — необязательный колбэк без аргументов; если он возвращает
    True, цикл прерывается между точками. Используется GUI для кнопки
    «Стоп»; при None (по умолчанию, как в CLI) не проверяется.

    results_sink — необязательный внешний список, куда точка кладётся сразу
    после измерения, а не по возвращении из функции. Нужен, чтобы уже
    снятые точки пережили аварийный останов: он обесточивает стенд из
    другого потока и закрывает сессии, после чего цикл падает, не успев
    ничего вернуть.

    suppress_notifications (п.38, галочка "отключить все предупреждения") —
    гасит только необязательный текст в логе (сейчас — предупреждение о
    перепутанной полярности, п.14); сами данные (PolarityMismatch, Rejected)
    записываются как обычно. Жёсткий запрет 800 А, аварийный останов и
    отсечка по погрешности этим флагом НЕ управляются — они не уведомления,
    а функции безопасности/измерения (см. PLAN_V2.md, п.38).

    max_attempts (баг-репорт п.12) — сколько раз ВСЕГО пытаться снять точку
    при выходе за порог погрешности, прежде чем забраковать (1 = без
    перепромеров, брак сразу; дефолт MAX_MEASUREMENT_ATTEMPTS). Отсечка/брак
    работают независимо от stop_on_error: stop_on_error решает лишь, ронять ли
    весь свип по факту брака, а сам брак (Rejected) ставится всегда, если
    известны ratio+I_nom и точка стабильно вне допуска.

    on_point_done (баг-репорт п.7) — необязательный колбэк (done, total),
    вызывается после каждой снятой точки; GUI пересчитывает по нему остаток
    времени по факту (с учётом перепромеров), а не по теоретической оценке.

    Возвращает (results, aborted_reason): aborted_reason — текст причины
    досрочной остановки по погрешности (после max_attempts подряд неудачных
    попыток, см. _measure_point_row), либо None, если свип прошёл до конца или
    был прерван пользователем.
    """
    if excitation_type == 'current':
        src.setup(voltage_limit=V_limit)
    elif excitation_type == 'voltage':
        src.setup(voltage_limit=X_stop, current_limit=I_limit)
    else:
        raise ValueError(f"Неизвестный тип возбуждения: {excitation_type!r} (ожидается 'current' или 'voltage')")

    plan = plan_override if plan_override is not None else plan_sweep(
        X_start, X_stop, X_step, branch=branch, preset=preset,
        zero_crossing_smooth=zero_crossing_smooth)
    averaging = dict(count=averaging_count, delay=averaging_delay, discard_first=discard_first)
    max_magnitude = max((p.magnitude for p in plan), default=0.0)
    y_sec_nom = (I_nom * turns / ratio) if (I_nom and ratio and ratio > 0) else None

    results: List[Dict] = []
    aborted_reason: Optional[str] = None
    current_relay_state: Optional[str] = None  # что реально сейчас установлено на плате
    run_started_fresh = True  # первая точка нового (после смены реле) прогона — под is_first в auto_range
    # Плавное нарастание (BETA): что реально стоит на источнике ПРЯМО
    # СЕЙЧАС, чтобы следующая точка знала, откуда набирать. 0.0 — источник
    # выключен/на нуле (старт сессии, после нулевой точки, после смены
    # полярности — во всех этих случаях набор идёт с нуля, не с прошлого
    # значения предыдущей ветви).
    ramp_from = 0.0

    try:
        for point in plan:
            if should_stop is not None and should_stop():
                _log("\nОстановка по запросу пользователя.", log_callback)
                break

            if point.relay != current_relay_state and point.relay is not None:
                if smooth_ramp:
                    # Переключать реле под током недопустимо — в обычном
                    # режиме выход уже гарантированно выключен к этому
                    # моменту (см. _measure_point_row), а в этом режиме
                    # источник мог остаться включённым с прошлой точки.
                    src.output_off()
                    ramp_from = 0.0
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
                row = _measure_zero_row(dmm, src, excitation_type, output_type, averaging, log_callback)
                ramp_from = 0.0  # источник выключен на нулевой точке — следующая начинает с нуля
            elif smooth_ramp and excitation_type == 'current':
                row, point_aborted = _measure_point_row_ramp(
                    dmm, src, output_type, point, ramp_from, ramp_duration,
                    ratio, turns, averaging, stop_on_error, error_threshold,
                    is_first_of_run=run_started_fresh, log_callback=log_callback,
                    zero_offset=zero_offset, suppress_notifications=suppress_notifications,
                    y_sec_nom=y_sec_nom, max_attempts=max_attempts,
                )
                run_started_fresh = False
                ramp_from = point.magnitude
                if point_aborted:
                    aborted_reason = point_aborted
            else:
                row, point_aborted = _measure_point_row(
                    dmm, src, excitation_type, output_type, point, delay, cooling_delay,
                    ratio, turns, averaging, stop_on_error, error_threshold,
                    is_first_of_run=run_started_fresh, log_callback=log_callback,
                    adaptive_cooling=adaptive_cooling, max_magnitude=max_magnitude,
                    adaptive_cooling_min_delay=adaptive_cooling_min_delay,
                    adaptive_cooling_max_delay=adaptive_cooling_max_delay,
                    suppress_notifications=suppress_notifications,
                    zero_offset=zero_offset,
                    y_sec_nom=y_sec_nom, max_attempts=max_attempts,
                )
                run_started_fresh = False
                if point_aborted:
                    aborted_reason = point_aborted

            results.append(row)
            if results_sink is not None:
                results_sink.append(row)

            # Прогресс-хук (баг-репорт п.7): сообщаем, сколько точек плана уже
            # снято — по факту, с учётом реально потраченного на перепромеры
            # времени; GUI по нему пересчитывает оставшуюся оценку (не по
            # теоретическому estimate_duration_seconds, который перепромеры не
            # знает). Ошибка в колбэке не должна валить измерение.
            if on_point_done is not None:
                try:
                    on_point_done(len(results), len(plan))
                except Exception:
                    pass

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


def estimate_duration_seconds(
    plan: List[SweepPoint],
    delay: float, cooling_delay: float,
    averaging_count: int = DEFAULT_AVERAGING_COUNT,
    averaging_delay: float = DEFAULT_AVERAGING_DELAY,
    adaptive_cooling: bool = False,
    adaptive_cooling_min_delay: float = DEFAULT_ADAPTIVE_COOLING_MIN_DELAY,
    adaptive_cooling_max_delay: float = DEFAULT_ADAPTIVE_COOLING_MAX_DELAY,
    smooth_ramp: bool = False,
    ramp_duration: float = 1.0,
) -> float:
    """
    Грубая оценка длительности измерения (п.15 — только для обратного
    отсчёта в GUI, никогда не используется в самом измерительном цикле).

    Считает по уже построенному плану (sweep.plan_sweep) те же паузы, что
    реально ждёт run_measurement() между действиями: delay после установки
    возбуждения, паузы усреднения (averaging_delay между отсчётами) и
    cooling_delay/адаптивную задержку после точки. НЕ учитывает: время
    самого VISA-обмена (запись уставки, чтение показаний — у разных
    приборов разное и заранее неизвестно) и повторные попытки при
    превышении погрешности (п.9 — по определению непредсказуемы заранее).
    Это оценка снизу, не гарантия точного времени.

    smooth_ramp/ramp_duration (BETA) — delay и cooling_delay в этом режиме
    не применяются вовсе (см. run_measurement/_measure_point_row_ramp) —
    вместо них на каждую ненулевую точку считается ramp_duration.
    """
    max_magnitude = max((p.magnitude for p in plan), default=0.0)
    total = 0.0
    for point in plan:
        # Усреднение идёт для любой точки, включая нулевую (см. _measure_zero_row).
        total += averaging_delay * max(0, averaging_count - 1)
        if point.is_zero:
            continue  # нулевая точка не проходит через delay/cooling_delay/ramp
        if smooth_ramp:
            total += ramp_duration
            continue
        total += delay
        if adaptive_cooling:
            total += _adaptive_cooling_delay(adaptive_cooling_min_delay, adaptive_cooling_max_delay,
                                             point.magnitude, max_magnitude)
        else:
            total += cooling_delay
    return total
