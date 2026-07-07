import time
from datetime import datetime
from typing import List, Dict

import pyvisa

from instruments import CurrentSource
from instruments import Multimeter as DMM


def run_measurement(dmm: DMM, src: CurrentSource,
                     I_start: float, I_stop: float, I_step: float,
                     V_limit: float, delay: float, cooling_delay: float,
                     direction: str, verbose: bool = True) -> List[Dict]:
    """
    Импульсный цикл измерения амплитудной характеристики датчика тока.

    На каждом шаге: устанавливается ток, включается выход, выдерживается delay,
    выполняется 3 измерения (с усреднением и авто-диапазоном), выход выключается,
    выдерживается cooling_delay.
    """
    sign = -1 if direction == 'negative' else 1
    num_steps = int(round((I_stop - I_start) / I_step)) + 1
    results = []

    src.setup(voltage_limit=V_limit)

    try:
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
                    # Вероятная перегрузка — переключаем диапазон вверх и повторяем попытку
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
                'I_set_A': signed_current,
                'I_meas_A': i_avg,
            })

            if verbose:
                print(f"  I_уст = {signed_current:+.4f} А  ->  I_изм = {i_avg:.6f} А")
    finally:
        src.shutdown()

    return results
