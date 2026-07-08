import time
from datetime import datetime
from typing import List, Dict

import pyvisa

from instruments import CurrentSource
from instruments import Multimeter as DMM
from relay import RelayController


def _measure_branch(dmm: DMM, src: CurrentSource,
                     I_start: float, I_stop: float, I_step: float,
                     delay: float, cooling_delay: float,
                     sign: int, branch_name: str,
                     range_reset: bool = False) -> List[Dict]:
    """
    Выполняет один проход измерения (0..I_max) для уже установленного реле
    (направление задаётся снаружи через relay.forward()/reverse()).
    sign используется только для записи знака в I_set_A.
    """
    num_steps = int(round((I_stop - I_start) / I_step)) + 1
    results = []

    if range_reset:
        # При смене направления датчик перемагничивается заново, поэтому
        # выбор диапазона вольтметра лучше начать заново с первой точки.
        dmm.current_range_idx = len(dmm.ranges) - 1
        dmm.set_range(dmm.ranges[dmm.current_range_idx])

    for step in range(num_steps):
        abs_current = I_start + step * I_step
        signed_current = abs_current * sign

        src.set_current(abs_current)
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
            'I_set_A': signed_current,
            'I_meas_A': i_avg,
        })

        print(f"  [{branch_name}] I_уст = {signed_current:+.4f} А  ->  I_изм = {i_avg:.6f} А")

    return results


def run_measurement(dmm: DMM, src: CurrentSource, relay: RelayController,
                     I_start: float, I_stop: float, I_step: float,
                     V_limit: float, delay: float, cooling_delay: float) -> List[Dict]:
    """
    Полный двусторонний цикл измерения амплитудной характеристики датчика тока
    с автоматическим переключением полярности через плату реле:

        1) relay.forward() -> проход 0..I_max (положительная ветвь, sign=+1)
        2) relay.reverse() -> проход 0..I_max (отрицательная ветвь, sign=-1)
        3) relay.off()

    Направление (Branch) сохраняется в каждой записи результата вместо
    прежнего единого параметра direction на весь запуск.
    """
    src.setup(voltage_limit=V_limit)
    results = []

    try:
        print("\nПереключаю реле: прямое направление (IFW)...")
        resp = relay.forward()
        print(f"  Ответ реле: {resp}")
        results += _measure_branch(
            dmm, src, I_start, I_stop, I_step, delay, cooling_delay,
            sign=+1, branch_name='forward',
        )

        print("\nПереключаю реле: обратное направление (IRW)...")
        resp = relay.reverse()
        print(f"  Ответ реле: {resp}")
        results += _measure_branch(
            dmm, src, I_start, I_stop, I_step, delay, cooling_delay,
            sign=-1, branch_name='reverse', range_reset=True,
        )
    finally:
        src.shutdown()
        relay.off()

    return results
