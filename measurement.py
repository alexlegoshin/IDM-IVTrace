import time
from typing import List, Dict
import pyvisa

from instruments import CurrentSource
from instruments import Multimeter as DMM

def run_measurement(dmm: DMM, src: CurrentSource,
                    I_start: float, I_stop: float, I_step: float,
                    V_limit: float, delay: float, cooling_delay: float,
                    direction: str) -> List[Dict]:
    sign = -1 if direction == 'negative' else 1
    num_steps = int((I_stop - I_start) / I_step) + 1
    results = []

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
                # Попытка переключить диапазон вверх
                if dmm.current_range_idx < len(dmm.ranges) - 1:
                    dmm.current_range_idx += 1
                    dmm.set_range(dmm.ranges[dmm.current_range_idx])
                    try:
                        i = dmm.measure_current()
                        currents.append(i)
                    except:
                        continue
            except Exception:
                pass

        i_avg = sum(currents) / len(currents) if currents else 0.0
        dmm.auto_range(i_avg, is_first=(step == 0))

        src.output_off()
        time.sleep(cooling_delay)

        results.append({
            'Timestamp': datetime.now().isoformat(),
            'I_set_A': signed_current,
            'I_meas_A': i_avg
        })
    src.shutdown()
    return results
