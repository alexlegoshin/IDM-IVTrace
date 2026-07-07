import json
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pyvisa


class Multimeter:
    """Обёртка над вольтметром/мультиметром, измеряющим ток (АКИП-2101, АКИП-B7-78/1 и т.п.)."""

    def __init__(self, resource_addr: str, config_path: Path, rm: Optional[pyvisa.ResourceManager] = None):
        self.config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.rm = rm or pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(resource_addr)
        self.instr.encoding = self.config.get('encoding', 'utf-8')
        self.instr.timeout = self.config.get('timeout', 5000)
        self.ranges = self.config['ranges']
        self.current_range_idx = len(self.ranges) - 1  # начинаем с максимального
        self._init_device()

    def _init_device(self):
        for cmd in self.config['init_commands']:
            self.instr.write(cmd)
            time.sleep(0.5 if cmd.strip() == '*RST' else 0.1)
        # Устанавливаем начальный (максимальный) диапазон
        self.set_range(self.ranges[self.current_range_idx])

    def set_range(self, range_val: float):
        self.instr.write(f'SENS:CURR:DC:RANG {range_val}')

    def measure_current(self) -> float:
        cmd = self.config['measure_command']
        return float(self.instr.query(cmd))

    def auto_range(self, measured_current: float, is_first: bool = False):
        """
        Динамическая подстройка диапазона по модулю измеренного тока.
        При is_first=True выбирается наименьший диапазон, покрывающий измеренное значение.
        Иначе — подъём при >95% предела, спуск при <10% предела.
        """
        abs_i = abs(measured_current)
        if is_first:
            for i, r in enumerate(self.ranges):
                if r >= abs_i:
                    self.current_range_idx = i
                    self.set_range(r)
                    return
            # Ток больше всех известных пределов — остаёмся на максимальном
            self.current_range_idx = len(self.ranges) - 1
            self.set_range(self.ranges[self.current_range_idx])
        else:
            current_limit = self.ranges[self.current_range_idx]
            if abs_i > current_limit * 0.95:
                if self.current_range_idx < len(self.ranges) - 1:
                    self.current_range_idx += 1
                    self.set_range(self.ranges[self.current_range_idx])
            elif abs_i < current_limit * 0.1 and self.current_range_idx > 0:
                for i in reversed(range(self.current_range_idx)):
                    if self.ranges[i] >= abs_i:
                        self.current_range_idx = i
                        self.set_range(self.ranges[i])
                        break

    def close(self):
        try:
            self.instr.close()
        except Exception:
            pass


class CurrentSource:
    """Обёртка над источником тока (например ITECH IT-M)."""

    def __init__(self, resource_addr: str, config_path: Path, rm: Optional[pyvisa.ResourceManager] = None):
        self.config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.rm = rm or pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(resource_addr)
        self.instr.encoding = self.config.get('encoding', 'utf-8')
        self.instr.timeout = self.config.get('timeout', 5000)
        self._init_device()

    def _init_device(self):
        for cmd in self.config['init_commands']:
            self.instr.write(cmd)
            time.sleep(0.5 if cmd.strip() == '*RST' else 0.1)

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

    def close(self):
        try:
            self.instr.close()
        except Exception:
            pass


def find_config_for_idn(idn: str, config_dir: Path) -> Optional[Path]:
    """Ищет json-конфиг в config_dir (нерекурсивно), у которого keywords встречаются в строке IDN."""
    for json_file in sorted(Path(config_dir).glob("*.json")):
        cfg = json.loads(json_file.read_text(encoding='utf-8'))
        keywords = cfg.get("keywords", [])
        if any(kw.upper() in idn.upper() for kw in keywords):
            return json_file
    return None


def discover_instruments(
    multimeter_dir: Path,
    source_dir: Path,
    rm: Optional[pyvisa.ResourceManager] = None,
    query_timeout: int = 3000,
) -> Tuple[str, Path, str, Path]:
    """
    Перебирает все доступные VISA-ресурсы, опрашивает *IDN? и сопоставляет
    каждый ответ с json-конфигами мультиметров и источников тока.

    Возвращает (dmm_addr, dmm_config_path, src_addr, src_config_path).
    Бросает RuntimeError, если один из приборов не найден.
    """
    rm = rm or pyvisa.ResourceManager()
    resources = rm.list_resources()

    if len(resources) == 0:
        raise RuntimeError("Не найдено ни одного VISA-ресурса. Проверьте подключение и драйверы NI-VISA.")

    dmm_addr = dmm_cfg = None
    src_addr = src_cfg = None

    print("Поиск приборов...")
    for res in resources:
        try:
            instr = rm.open_resource(res)
            instr.encoding = 'utf-8'
            instr.timeout = query_timeout
            idn = instr.query('*IDN?').strip()
            print(f"  {res}  ->  {idn}")

            if dmm_addr is None:
                cfg = find_config_for_idn(idn, multimeter_dir)
                if cfg is not None:
                    dmm_addr, dmm_cfg = res, cfg

            if src_addr is None:
                cfg = find_config_for_idn(idn, source_dir)
                if cfg is not None:
                    src_addr, src_cfg = res, cfg

            instr.close()
        except Exception as e:
            print(f"  {res}  ->  Ошибка при опросе: {e}")

    if not dmm_addr or not src_addr:
        missing = []
        if not dmm_addr:
            missing.append("мультиметр")
        if not src_addr:
            missing.append("источник тока")
        raise RuntimeError(
            f"Не удалось обнаружить: {', '.join(missing)}. Проверьте список ресурсов выше и json-конфиги."
        )

    print(f"\nМультиметр:    {dmm_addr}  ({dmm_cfg.stem})")
    print(f"Источник тока: {src_addr}  ({src_cfg.stem})\n")

    return dmm_addr, dmm_cfg, src_addr, src_cfg
