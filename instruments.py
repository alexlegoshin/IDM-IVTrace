import json
import math
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pyvisa

# Реальный ток датчика на этом стенде никогда не приблизится к этому
# порядку величины. Значение такого масштаба (~9.9e37 у большинства
# SCPI-совместимых DMM) — это не измерение, а сентинел переполнения:
# прибор отдаёт его вместо ошибки, когда измеряемая величина выходит за
# пределы текущего установленного диапазона (см. issue #3 — крупный шаг
# развёртки на датчике с большим коэффициентом преобразования). Раньше это
# число принималось за обычное показание и попадало и в среднее, и в CSV.
OVERFLOW_SENTINEL_THRESHOLD = 1e30

# Пауза после смены диапазона в РУЧНОМ режиме (manual_range: true) — прибор
# физически переключает реле входного делителя и не успевает устояться
# мгновенно. В авто-режиме прибор делает это сам и синхронно с измерением,
# поэтому здесь эта пауза не участвует (см. Multimeter.set_range).
# Регулируется полем "range_settle_delay" в конфиге прибора.
DEFAULT_RANGE_SETTLE_DELAY = 0.7

# Гистерезис auto_range(): порог подъёма — доля от ВЕРХНЕГО (текущего)
# диапазона; порог спуска — доля от НИЖНЕГО (на ступень меньше текущего)
# диапазона. Раньше оба порога считались от текущего диапазона (95% вверх,
# 10% вниз) — а на декадных шкалах 10% текущего это ровно 100% диапазона
# на ступень ниже, то есть сразу после спуска показание оказывалось на
# самой верхней границе НОВОГО диапазона и тут же провоцировало обратный
# подъём (автоколебания). Проверка порога спуска по диапазону назначения,
# а не текущему, убирает этот нахлёст: после спуска значение занимает не
# больше DOWN_SAFE_FRACTION нового диапазона — заведомо ниже порога подъёма.
UP_THRESHOLD_FRACTION = 0.95
DOWN_SAFE_FRACTION = 0.5


def is_overflow_reading(value: float) -> bool:
    """True, если value похоже на SCPI-сентинел переполнения диапазона, а не на реальное измерение."""
    return not math.isnan(value) and abs(value) > OVERFLOW_SENTINEL_THRESHOLD


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
        # Ручной диапазон — опциональная возможность (manual_range в
        # конфиге, по умолчанию выключена — прибор остаётся на встроенном
        # авто-диапазоне). Не все приборы вообще поддерживают ручной SCPI-
        # диапазон (см. RIGOL DM3068 — -113 Undefined header на всю
        # подсистему SENS:), поэтому явное отключение автодиапазона
        # выполняется только когда ручной режим реально запрошен.
        if self.config.get('manual_range', False):
            disable_autorange_cmd = self.config.get('disable_autorange_command')
            if disable_autorange_cmd:
                self.instr.write(disable_autorange_cmd)
                time.sleep(0.1)
        # Устанавливаем начальный (максимальный) диапазон (no-op, если
        # ручной режим выключен — см. set_range()).
        self.set_range(self.ranges[self.current_range_idx])

    def set_range(self, range_val: float):
        if not self.config.get('manual_range', False):
            return
        # range_command может параметризоваться и значением диапазона
        # ({range_val}, большинство приборов, например АКИП: "...RANG
        # {range_val}"), и его ПОРЯДКОВЫМ ИНДЕКСОМ ({index}) — так у RIGOL
        # DM3068 в режиме вольтметра (":MEASure:VOLTage:DC {index}", индекс
        # 0..4, проверено в IDM-DNKMetr). Плейсхолдер, которого нет в
        # строке команды, str.format() просто игнорирует, поэтому оба можно
        # передавать всегда, не завися от того, какой стиль в конфиге.
        cmd = self.config['range_command']
        self.instr.write(cmd.format(range_val=range_val, index=self.current_range_idx))
        # Задержка на устаканивание — только в ручном режиме: в авто прибор
        # переключает диапазон сам, синхронно с собственным измерением, и
        # эта пауза ему ни к чему (см. DEFAULT_RANGE_SETTLE_DELAY выше).
        delay = self.config.get('range_settle_delay', DEFAULT_RANGE_SETTLE_DELAY)
        if delay > 0:
            time.sleep(delay)

    def measure_current(self) -> float:
        # В ручном режиме (manual_range: true) measure_command обязан быть
        # 'READ?' (или 'FETC?'), а не 'MEAS:CURR:DC?': MEAS?/CONF? по SCPI
        # переконфигурируют прибор и сбрасывают диапазон обратно в AUTO при
        # каждом вызове, из-за чего set_range()/auto_range() становятся
        # no-op. В авто-режиме (по умолчанию) это ограничение не действует.
        cmd = self.config['measure_command']
        return float(self.instr.query(cmd))

    def _covering_range_index(self, abs_value: float) -> int:
        """Наименьший индекс диапазона, покрывающего |value|; максимум, если такого нет."""
        for i, r in enumerate(self.ranges):
            if r >= abs_value:
                return i
        return len(self.ranges) - 1

    def auto_range(self, measured_current: float, is_first: bool = False):
        """
        Подстройка диапазона по модулю measured_current.

        is_first=True — диапазон выбирается заново, без оглядки на текущий:
        наименьший, покрывающий |measured_current|. Используется не только
        для самой первой точки прохода, но и как ПРЕДСКАЗАНИЕ диапазона под
        СЛЕДУЮЩУЮ точку по её ожидаемому значению (X_set/ratio) — см.
        measurement.py. Раньше диапазон выбирался только по факту ПРЕДЫДУЩЕЙ
        точки, и на резком скачке возбуждения (issue #3) заведомо не
        подходил для новой, гораздо большей точки.

        is_first=False — плавная подстройка вокруг уже выбранного диапазона:
        подъём на одну ступень при >95% его предела, спуск на одну ступень,
        если значение уместится не более чем в DOWN_SAFE_FRACTION диапазона
        НИЖЕ текущего (не самого текущего — см. DOWN_SAFE_FRACTION выше).
        Одной ступени вверх достаточно: значения, которым нужно больше,
        перехватываются как переполнение (is_overflow_reading) ДО вызова
        auto_range() и обрабатываются прыжком на максимум отдельно — сюда
        такие значения не попадают вовсе (см. measurement.py).
        """
        abs_i = abs(measured_current)
        if is_first:
            self.current_range_idx = self._covering_range_index(abs_i)
            self.set_range(self.ranges[self.current_range_idx])
            return

        current_limit = self.ranges[self.current_range_idx]
        if abs_i > current_limit * UP_THRESHOLD_FRACTION:
            if self.current_range_idx < len(self.ranges) - 1:
                self.current_range_idx += 1
                self.set_range(self.ranges[self.current_range_idx])
        elif self.current_range_idx > 0:
            lower_limit = self.ranges[self.current_range_idx - 1]
            if abs_i <= lower_limit * DOWN_SAFE_FRACTION:
                self.current_range_idx -= 1
                self.set_range(self.ranges[self.current_range_idx])

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


class VoltageSource:
    """
    Обёртка над программируемым источником напряжения (GW Instek GPP-серия),
    работающим в режиме Tracking Series (CH1 master + CH2 slave, без общей
    точки) для получения объединённого диапазона 0..64В.

    Все команды соответствуют официальной документации GW Instek
    (GPP-Series_User_manual_EN_REVG_20240506.pdf, стр. 128, 133-135):
      TRACK1               — включить tracking series
      VSET<x>:<value>      — задать напряжение канала x (используется CH1=master)
      ISET<x>:<value>      — задать токоограничение канала x
      :OUTPut<x>:STATe ON  — включить выход канала x
      VOUT<x>? / IOUT<x>?  — измеренные (фактические) значения канала x

    ВАЖНО: IDN? прибора этой серии возвращает модель БЕЗ ведущей цифры,
    например GPP-74323 представляется как "GPP-4323".
    """

    def __init__(self, resource_addr: str, config_path: Path, rm: Optional[pyvisa.ResourceManager] = None):
        self.config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.rm = rm or pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(resource_addr)
        self.instr.encoding = self.config.get('encoding', 'utf-8')
        self.instr.timeout = self.config.get('timeout', 5000)
        self.primary_ch = self.config.get('channels', {}).get('primary', 1)
        self._init_device()

    def _init_device(self):
        for cmd in self.config['init_commands']:
            self.instr.write(cmd)
            time.sleep(0.5 if cmd.strip() == '*RST' else 0.1)
        # Объединяем CH1(master)+CH2(slave) в tracking series: 0..64В на
        # клеммах CH1(+) и CH2(-), без общей точки. Управление — только
        # через CH1 (master); CH2 в этом режиме недоступен для настройки.
        self.instr.write(self.config['tracking_series_command'])
        time.sleep(0.2)

    def setup(self, voltage_limit: float, current_limit: float = 1.0):
        """
        voltage_limit принимается для единообразия вызова с CurrentSource.setup()
        (measurement.py вызывает src.setup(voltage_limit=...) для обоих типов
        источника), но сейчас не используется: в конфиге GPP-серии нет
        отдельной команды OVP/предела по напряжению — сама уставка (VSET)
        каждый раз ограничена X_stop. current_limit — реальное ограничение
        по току (защита источника через ISET).
        """
        cmds = self.config['setup_commands']
        self.instr.write(cmds['current_limit'].format(ch=self.primary_ch, current=current_limit))
        self.instr.write(cmds['voltage'].format(ch=self.primary_ch, voltage=0))

    def set_voltage(self, voltage: float):
        cmd = self.config['setup_commands']['voltage'].format(ch=self.primary_ch, voltage=voltage)
        self.instr.write(cmd)

    def output_on(self):
        self.instr.write(self.config['output_on'].format(ch=self.primary_ch))

    def output_off(self):
        self.instr.write(self.config['output_off'].format(ch=self.primary_ch))

    def shutdown(self):
        self.set_voltage(0)
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
    source_label: str = "источник",
) -> Tuple[str, Path, str, Path]:
    """
    Перебирает все доступные VISA-ресурсы, опрашивает *IDN? и сопоставляет
    каждый ответ с json-конфигами мультиметров и источников (тип источника —
    ток или напряжение — определяется тем, какая source_dir передана).

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
            missing.append(source_label)
        raise RuntimeError(
            f"Не удалось обнаружить: {', '.join(missing)}. Проверьте список ресурсов выше и json-конфиги."
        )

    print(f"\nМультиметр: {dmm_addr}  ({dmm_cfg.stem})")
    print(f"{source_label.capitalize()}: {src_addr}  ({src_cfg.stem})\n")

    return dmm_addr, dmm_cfg, src_addr, src_cfg
