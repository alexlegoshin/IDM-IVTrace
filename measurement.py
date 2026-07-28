import math
import time
from datetime import datetime
from typing import Callable, List, Dict, Optional, Union, Tuple

import pyvisa

from instruments import CurrentSource, VoltageSource
from instruments import Multimeter as DMM
from relay import RelayController

# Единицы измерения задаваемой величины возбуждения — используются и в
# именах колонок CSV, и в подписях графиков analysis.py.
EXCITATION_UNITS = {
    'current': 'A',
    'voltage': 'V',
}


def _measure_branch(dmm: DMM, src: Union[CurrentSource, VoltageSource],
                     excitation_type: str,
                     X_start: float, X_stop: float, X_step: float,
                     delay: float, cooling_delay: float,
                     sign: int, branch_name: str,
                     range_reset: bool = False,
                     should_stop: Optional[Callable[[], bool]] = None,
                     # Новые параметры для контроля погрешности и инверсии
                     I_nom: Optional[float] = None,
                     ratio: Optional[float] = None,
                     stop_on_error: bool = False,
                     error_threshold: float = 1.0,
                     invert_input: bool = False,
                     log_callback: Optional[Callable[[str], None]] = None) -> Tuple[List[Dict], Optional[str]]:
    """
    Выполняет один проход измерения (0..X_max) для уже установленного реле.

    Возвращает (results, aborted_reason), где aborted_reason — строка с причиной
    прерывания (если цикл был остановлен из-за превышения погрешности) или None.
    """
    num_steps = int(round((X_stop - X_start) / X_step)) + 1
    results = []
    aborted_reason = None

    if range_reset:
        dmm.current_range_idx = len(dmm.ranges) - 1
        dmm.set_range(dmm.ranges[dmm.current_range_idx])

    for step in range(num_steps):
        if should_stop is not None and should_stop():
            print(f"  [{branch_name}] Остановка по запросу пользователя.")
            break

        abs_value = X_start + step * X_step
        signed_value = abs_value * sign

        if excitation_type == 'current':
            src.set_current(abs_value)
        else:
            src.set_voltage(abs_value)
        src.output_on()
        time.sleep(delay)

        currents = []
        for _ in range(3):
            try:
                i = dmm.measure_current()
                currents.append(i)
            except pyvisa.errors.VisaIOError:
                if dmm.current_range_idx < len(dmm.ranges) - 1:
                    dmm.current_range_idx += 1
                    dmm.set_range(dmm.ranges[dmm.current_range_idx])
                    try:
                        i = dmm.measure_current()
                        currents.append(i)
                    except Exception:
                        pass
            except Exception:
                pass

        if currents:
            i_avg = sum(currents) / len(currents)
            dmm.auto_range(i_avg, is_first=(step == 0))
        else:
            i_avg = math.nan

        src.output_off()
        time.sleep(cooling_delay)

        # Проверка погрешности в реальном времени
        if stop_on_error and ratio and ratio > 0 and not math.isnan(i_avg):
            # Ожидаемый выходной ток: I_expected = abs(X_set) / ratio
            X_abs = abs(signed_value)
            if X_abs > 0:
                I_expected = X_abs / ratio
                error_percent = abs((i_avg - I_expected) / I_expected) * 100.0
                if error_percent > error_threshold:
                    aborted_reason = (f"Погрешность {error_percent:.2f}% превысила порог {error_threshold}% "
                                      f"на X_set = {signed_value:.4f} {EXCITATION_UNITS[excitation_type]}")
                    if log_callback:
                        log_callback(f"  [{branch_name}] {aborted_reason} — точка не записана, измерение прервано.")
                    else:
                        print(f"  [{branch_name}] {aborted_reason} — точка не записана, измерение прервано.")
                    # Не добавляем точку, выходим из цикла
                    break

        # Если инверсия включена, меняем знак X_set при записи (но не влияем на физическое возбуждение)
        recorded_X = -signed_value if invert_input else signed_value

        results.append({
            'Timestamp': datetime.now().isoformat(),
            'Branch': branch_name,
            'X_set': recorded_X,
            'I_meas_A': i_avg,
        })

        unit = EXCITATION_UNITS[excitation_type]
        msg = f"  [{branch_name}] X_уст = {signed_value:+.4f} {unit}  ->  I_изм = {i_avg:.6f} А"
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    return results, aborted_reason


def run_measurement(dmm: DMM, src: Union[CurrentSource, VoltageSource], relay: RelayController,
                     excitation_type: str,
                     X_start: float, X_stop: float, X_step: float,
                     V_limit: float, delay: float, cooling_delay: float,
                     should_stop: Optional[Callable[[], bool]] = None,
                     # Новые параметры
                     I_nom: Optional[float] = None,
                     ratio: Optional[float] = None,
                     stop_on_error: bool = False,
                     error_threshold: float = 1.0,
                     use_relay: bool = True,
                     invert_input: bool = False,
                     log_callback: Optional[Callable[[str], None]] = None) -> Tuple[List[Dict], Optional[str]]:
    """
    Полный цикл измерения амплитудной характеристики датчика.

    Если use_relay == False, измеряется только прямое направление (без реле).
    Возвращает (results, aborted_reason).
    """
    if excitation_type == 'current':
        src.setup(voltage_limit=V_limit)
    elif excitation_type == 'voltage':
        src.setup(voltage_limit=X_stop)
    else:
        raise ValueError(f"Неизвестный тип возбуждения: {excitation_type!r} (ожидается 'current' или 'voltage')")

    results = []
    aborted_reason = None

    try:
        if use_relay:
            print("\nПереключаю реле: прямое направление (IFW)...")
            resp = relay.forward()
            print(f"  Ответ реле: {resp}")

        branch_results, branch_aborted = _measure_branch(
            dmm, src, excitation_type, X_start, X_stop, X_step, delay, cooling_delay,
            sign=+1, branch_name='forward',
            should_stop=should_stop,
            I_nom=I_nom, ratio=ratio,
            stop_on_error=stop_on_error, error_threshold=error_threshold,
            invert_input=invert_input,
            log_callback=log_callback,
        )
        results.extend(branch_results)
        if branch_aborted:
            aborted_reason = branch_aborted
            return results, aborted_reason

        if should_stop is not None and should_stop():
            print("\nИзмерение прервано пользователем до обратной ветви.")
            return results, None

        if use_relay:
            print("\nПереключаю реле: обратное направление (IRW)...")
            resp = relay.reverse()
            print(f"  Ответ реле: {resp}")
            branch_results, branch_aborted = _measure_branch(
                dmm, src, excitation_type, X_start, X_stop, X_step, delay, cooling_delay,
                sign=-1, branch_name='reverse', range_reset=True,
                should_stop=should_stop,
                I_nom=I_nom, ratio=ratio,
                stop_on_error=stop_on_error, error_threshold=error_threshold,
                invert_input=invert_input,
                log_callback=log_callback,
            )
            results.extend(branch_results)
            if branch_aborted:
                aborted_reason = branch_aborted
                return results, aborted_reason

    finally:
        src.shutdown()
        if use_relay:
            relay.off()

    return results, aborted_reason
