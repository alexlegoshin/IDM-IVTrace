import json
import math
import re
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pyvisa

# Некоторые приборы (GW Instek GPP-серия — VOUT1?/IOUT1?) отвечают на
# измерительные запросы с суффиксом единиц ("00.000V", "0.0000A"), а не
# голым числом — plain float() на таком ответе падает (см. IDM-DNKMetr).
_NUMBER_RE = re.compile(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?')


def parse_scpi_number(text: str) -> float:
    """Извлекает число из ответа прибора, даже если оно снабжено суффиксом единиц."""
    match = _NUMBER_RE.search(text)
    if match is None:
        raise ValueError(f"Не удалось извлечь число из ответа прибора: {text!r}")
    return float(match.group())

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
        # Не все приборы довольствуются VISA-умолчаниями терминатора строки
        # (см. RIGOL DM3068 в режиме вольтметра — требует явных '\n' на
        # запись и на чтение). Поле необязательное — большинству USB-TMC
        # приборов оно не нужно вовсе.
        if 'write_termination' in self.config:
            self.instr.write_termination = self.config['write_termination']
        if 'read_termination' in self.config:
            self.instr.read_termination = self.config['read_termination']
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

    def measure(self) -> float:
        """
        Снимает одно показание по measure_command из конфига — тока,
        напряжения или чего угодно ещё, чем прибор настроен измерять
        (SENS:FUNC в init_commands). Класс сам единиц измерения не знает и
        не проверяет: это ответственность конфига и вызывающего кода.

        В ручном режиме (manual_range: true) measure_command обязан быть
        'READ?' (или 'FETC?'), а не 'MEAS:CURR:DC?'/'MEAS:VOLT:DC?': MEAS?/
        CONF? по SCPI переконфигурируют прибор и сбрасывают диапазон обратно
        в AUTO при каждом вызове, из-за чего set_range()/auto_range()
        становятся no-op. В авто-режиме (по умолчанию) это ограничение не
        действует.
        """
        cmd = self.config['measure_command']
        return parse_scpi_number(self.instr.query(cmd))

    def measure_current(self) -> float:
        """Историческое имя measure() — сохранено для обратной совместимости с измерительным циклом (см. measurement.py)."""
        return self.measure()

    def measure_voltage(self) -> float:
        """Алиас measure() для семантической ясности, когда прибор явно настроен как вольтметр (см. п.4)."""
        return self.measure()

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

    ВАЖНО (см. IDM-DNKMetr): подключение — не USB-TMC, а ASRL (COM-порт)
    через vendor-specific USB-serial мост (VID_2184 — обычный CH340/CH341
    CDC-драйвер не подходит, нужен официальный драйвер GW Instek), скорость
    115200 8N1. Официально проверены вживую: ALLOUTON/ALLOUTOFF (глобальное
    включение/выключение выхода — а НЕ :OUTPut{ch}:STATe ON/OFF, который
    никогда не тестировался на реальном приборе) и то, что VOUT?/IOUT?
    отвечают С СУФФИКСОМ ЕДИНИЦ ("00.000V", "0.0000A") — обычный float() на
    таком ответе падает, см. parse_scpi_number() выше.
    """

    def __init__(self, resource_addr: str, config_path: Path, rm: Optional[pyvisa.ResourceManager] = None):
        self.config = json.loads(Path(config_path).read_text(encoding='utf-8'))
        self.rm = rm or pyvisa.ResourceManager()
        self.instr = self.rm.open_resource(resource_addr)
        self.instr.encoding = self.config.get('encoding', 'utf-8')
        self.instr.timeout = self.config.get('timeout', 5000)
        if 'baud_rate' in self.config:
            # ASRL-приборы не подхватывают нужную скорость сами (GW Instek
            # GPP-серия требует 115200, а не типичные VISA-умолчания). Поле
            # необязательное — для USB-TMC/GPIB источников в конфиге его
            # просто нет, и эта строка ничего не делает.
            self.instr.baud_rate = self.config['baud_rate']
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

    def setup(self, voltage_limit: float, current_limit: float):
        """
        voltage_limit принимается для единообразия вызова с CurrentSource.setup()
        (measurement.py вызывает src.setup(voltage_limit=...) для обоих типов
        источника), но сейчас не используется: в конфиге GPP-серии нет
        отдельной команды OVP/предела по напряжению — сама уставка (VSET)
        каждый раз ограничена X_stop. current_limit — реальное ограничение
        по току (защита источника через ISET); обязателен, без дефолта —
        значение всегда приходит от оператора (I_limit, см. measurement.py/
        orchestrate.py), молчаливая подстановка тут была бы опасным хардкодом.
        """
        cmds = self.config['setup_commands']
        self.instr.write(cmds['current_limit'].format(ch=self.primary_ch, current=current_limit))
        self.instr.write(cmds['voltage'].format(ch=self.primary_ch, voltage=0))

    def set_voltage(self, voltage: float):
        cmd = self.config['setup_commands']['voltage'].format(ch=self.primary_ch, voltage=voltage)
        self.instr.write(cmd)

    def output_on(self):
        # ALLOUTON — проверено вживую в IDM-DNKMetr, приоритетный вариант.
        # Резервный :OUTPut{ch}:STATe ON никогда не тестировался на реальном
        # GPP-4323 (см. docstring класса) — оставлен только для конфигов,
        # которые ALLOUTON/ALLOUTOFF не объявляют вовсе.
        cmd = self.config.get('all_output_on')
        if cmd is None:
            cmd = self.config['output_on'].format(ch=self.primary_ch)
        self.instr.write(cmd)

    def output_off(self):
        cmd = self.config.get('all_output_off')
        if cmd is None:
            cmd = self.config['output_off'].format(ch=self.primary_ch)
        self.instr.write(cmd)

    def measure_voltage(self) -> float:
        """Фактическое напряжение на выходе (VOUT?) — не путать с уставкой set_voltage()."""
        cmd = self.config['measure_voltage_command'].format(ch=self.primary_ch)
        return parse_scpi_number(self.instr.query(cmd))

    def measure_current(self) -> float:
        """Фактический ток на выходе (IOUT?)."""
        cmd = self.config['measure_current_command'].format(ch=self.primary_ch)
        return parse_scpi_number(self.instr.query(cmd))

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


def identify_instrument(rm: pyvisa.ResourceManager, address: str, config: dict) -> bool:
    """
    «Мигнуть» прибором (п.11) — оператор выбрал адрес в выпадающем списке
    (п.12) и хочет физически убедиться, какой это прибор на столе.

    Читает необязательное поле `identify_command` из конфига и, если оно
    есть, один раз отправляет его выбранному прибору. Ни один из
    json-конфигов в этом репозитории сейчас такую команду не объявляет —
    универсальной SCPI-команды "мигни" не существует, а какая конкретно
    команда (если вообще есть) работает на каждой модели стенда, не
    проверялось на реальном железе (см. PLAN_V2.md: не сочинять
    неподтверждённые SCPI-команды). Отсутствие поля — не ошибка, просто
    "для этого прибора не настроено"; True/False сообщает вызывающей
    стороне (GUI), показывать ли операцию как выполненную или как
    неподдерживаемую.
    """
    cmd = config.get('identify_command')
    if not cmd:
        return False
    instr = rm.open_resource(address)
    try:
        instr.encoding = config.get('encoding', 'utf-8')
        instr.timeout = config.get('timeout', 5000)
        instr.write(cmd)
    finally:
        instr.close()
    return True


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
