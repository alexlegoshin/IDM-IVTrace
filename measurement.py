import time
from datetime import datetime
from typing import List, Dict, Union

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
                     range_reset: bool = False) -> List[Dict]:
    """
    Выполняет один проход измерения (0..X_max) для уже установленного реле
    (направление задаётся снаружи через relay.forward()/reverse()).

    excitation_type определяет, что именно выставляется на источнике —
    ток (src.set_current) или напряжение (src.set_voltage). Измеряемая
    датчиком величина всегда ток (см. измерение в run_measurement).
    sign используется только для записи знака в X_set.
    """
    num_steps = int(round((X_stop - X_start) / X_step)) + 1
    results = []

    if range_reset:
        # При смене направления датчик перемагничивается заново, поэтому
        # выбор диапазона вольтметра лучше начать заново с первой точки.
        dmm.current_range_idx = len(dmm.ranges) - 1
        dmm.set_range(dmm.ranges[dmm.current_range_idx])

    for step in range(num_steps):
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

        i_avg = sum(currents) / len(currents) if currents else 0.0
        dmm.auto_range(i_avg, is_first=(step == 0))

        src.output_off()
        time.sleep(cooling_delay)

        results.append({
            'Timestamp': datetime.now().isoformat(),
            'Branch': branch_name,
            'X_set': signed_value,
            'I_meas_A': i_avg,
        })

        unit = EXCITATION_UNITS[excitation_type]
        print(f"  [{branch_name}] X_уст = {signed_value:+.4f} {unit}  ->  I_изм = {i_avg:.6f} А")

    return results


def run_measurement(dmm: DMM, src: Union[CurrentSource, VoltageSource], relay: RelayController,
                     excitation_type: str,
                     X_start: float, X_stop: float, X_step: float,
                     V_limit: float, delay: float, cooling_delay: float) -> List[Dict]:
    """
    Полный двусторонний цикл измерения амплитудной характеристики датчика тока
    с автоматическим переключением полярности через плату реле:

        1) relay.forward() -> проход 0..X_max (положительная ветвь, sign=+1)
        2) relay.reverse() -> проход 0..X_max (отрицательная ветвь, sign=-1)
        3) relay.off()

    excitation_type: 'current' — на источник тока подаётся уставка тока
                      (V_limit используется как ограничение по напряжению);
                      'voltage' — на источник напряжения подаётся уставка
                      напряжения (V_limit в этом случае не используется для
                      настройки источника, X_stop и есть максимальное
                      напряжение цикла).

    Выход датчика (измеряемая величина) всегда ток — читается мультиметром
    независимо от типа возбуждения.

    Направление (Branch) сохраняется в каждой записи результата.
    """
    if excitation_type == 'current':
        src.setup(voltage_limit=V_limit)
    elif excitation_type == 'voltage':
        src.setup(voltage_limit=X_stop)
    else:
        raise ValueError(f"Неизвестный тип возбуждения: {excitation_type!r} (ожидается 'current' или 'voltage')")

    results = []

    try:
        print("\nПереключаю реле: прямое направление (IFW)...")
        resp = relay.forward()
        print(f"  Ответ реле: {resp}")
        results += _measure_branch(
            dmm, src, excitation_type, X_start, X_stop, X_step, delay, cooling_delay,
            sign=+1, branch_name='forward',
        )

        print("\nПереключаю реле: обратное направление (IRW)...")
        resp = relay.reverse()
        print(f"  Ответ реле: {resp}")
        results += _measure_branch(
            dmm, src, excitation_type, X_start, X_stop, X_step, delay, cooling_delay,
            sign=-1, branch_name='reverse', range_reset=True,
        )
    finally:
        src.shutdown()
        relay.off()

    return results
