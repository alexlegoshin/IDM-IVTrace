import json
from pathlib import Path
import pyvisa
import time
from typing import List, Optional

class Multimeter:
    def __init__(self, resource_addr: str, config_path: Path):
        self.config = json.loads(config_path.read_text(encoding='utf-8'))
        self.rm = pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(resource_addr)
        self.instr.encoding = self.config.get('encoding', 'utf-8')
        self.instr.timeout = self.config.get('timeout', 5000)
        self.ranges = self.config['ranges']
        self.current_range_idx = len(self.ranges) - 1  # начинаем с максимального
        self._init_device()

    def _init_device(self):
        for cmd in self.config['init_commands']:
            self.instr.write(cmd)
            time.sleep(0.1)  # небольшая пауза на всякий случай
        # Устанавливаем начальный (максимальный) диапазон
        self.set_range(self.ranges[self.current_range_idx])

    def set_range(self, range_val: float):
        self.instr.write(f'SENS:CURR:DC:RANG {range_val}')

    def measure_current(self) -> float:
        cmd = self.config['measure_command']
        return float(self.instr.query(cmd))

    def auto_range(self, measured_current: float, is_first: bool = False):
        # Логика без изменений
        abs_i = abs(measured_current)
        if is_first:
            for i, r in enumerate(self.ranges):
                if r >= abs_i:
                    self.current_range_idx = i
                    self.set_range(r)
                    break
        else:
            if abs_i > self.ranges[self.current_range_idx] * 0.95:
                if self.current_range_idx < len(self.ranges) - 1:
                    self.current_range_idx += 1
                    self.set_range(self.ranges[self.current_range_idx])
            elif abs_i < self.ranges[self.current_range_idx] * 0.1 and self.current_range_idx > 0:
                for i in reversed(range(self.current_range_idx)):
                    if self.ranges[i] >= abs_i:
                        self.current_range_idx = i
                        self.set_range(self.ranges[i])
                        break

class CurrentSource:
    def __init__(self, resource_addr: str, config_path: Path):
        self.config = json.loads(config_path.read_text(encoding='utf-8'))
        self.rm = pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(resource_addr)
        self.instr.encoding = self.config.get('encoding', 'utf-8')
        self.instr.timeout = self.config.get('timeout', 5000)
        self._init_device()

    def _init_device(self):
        for cmd in self.config['init_commands']:
            self.instr.write(cmd)
            time.sleep(0.5)

    def setup(self, voltage_limit: float, slew_rate: float = 10.0):
        cmds = self.config['setup_commands']
        self.instr.write(cmds['voltage_limit'].format(voltage=voltage_limit))
        self.instr.write(cmds['current'].format(current=0))
        if 'slew_rate' in cmds:
            self.instr.write(cmds['slew_rate'].format(rate=slew_rate))

    def set_current(self, current: float):
        self.instr.write(self.config['setup_commands']['current'].format(current=current))

    def output_on(self):
        self.instr.write(self.config['output_on'])

    def output_off(self):
        self.instr.write(self.config['output_off'])

    def shutdown(self):
        self.set_current(0)
        self.output_off()

def find_config_for_idn(idn: str, config_dir: Path) -> Optional[Path]:
    for json_file in config_dir.glob("*.json"):
        cfg = json.loads(json_file.read_text(encoding='utf-8'))
        keywords = cfg.get("keywords", [])
        if any(kw.upper() in idn.upper() for kw in keywords):
            return json_file
    return None
