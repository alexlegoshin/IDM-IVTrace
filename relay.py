"""
Управление платой релейного коммутатора полярности тока.

Протокол (обнаружен экспериментально, см. tests.ipynb):
- Serial, 115200 бод, окончание строки \r\n.
- После открытия порта плата ~5 c грузится (эхо загрузчика PI_FAST_FLASH_BOOT...),
  затем готова принимать команды.
- Команды (без аргументов, каждая отвечает 'OK'):
    BEN  — проверка связи / статус жгутов
    IFW  — включить ток в прямом направлении
    IRW  — включить ток в обратном направлении
    I_0  — отключить ток (реле в исходное состояние)
"""
import time
from typing import List, Optional

import serial
from serial.tools import list_ports

BAUDRATE = 115200
BOOT_DELAY = 10.0       # время на загрузку контроллера после открытия порта
CMD_DELAY = 1.0        # пауза после отправки команды перед чтением ответа
RESPONSE_TIMEOUT = 1.0


class RelayController:
    """Обёртка над платой релейного переключателя направления тока."""

    def __init__(self, port: str, wait_for_boot: bool = True):
        self.port = port
        self.ser = serial.Serial(port, baudrate=BAUDRATE, timeout=RESPONSE_TIMEOUT)
        if wait_for_boot:
            time.sleep(BOOT_DELAY)
        self.ser.reset_input_buffer()

    def _send(self, cmd: str) -> str:
        self.ser.write(cmd.encode('utf-8') + b'\r\n')
        time.sleep(CMD_DELAY)
        raw = self.ser.read(self.ser.in_waiting or 1)
        return raw.decode('utf-8', errors='ignore').strip()

    def check(self) -> str:
        """Отправляет BEN и возвращает ответ платы (статус жгутов)."""
        return self._send('BEN')

    def forward(self) -> str:
        """Включает ток в прямом направлении (IFW)."""
        return self._send('IFW')

    def reverse(self) -> str:
        """Включает ток в обратном направлении (IRW)."""
        return self._send('IRW')

    def off(self) -> str:
        """Отключает ток, реле возвращается в исходное состояние (I_0)."""
        return self._send('I_0')

    def close(self):
        try:
            self.off()
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass


def list_candidate_ports() -> List[str]:
    """Возвращает список доступных COM/serial-портов в системе."""
    return [p.device for p in list_ports.comports()]


def discover_relay_port(candidate_ports: Optional[List[str]] = None) -> str:
    """
    Перебирает доступные serial-порты, пытаясь на каждом дождаться загрузки
    платы и получить осмысленный ответ на BEN (содержащий 'OK').

    Возвращает имя найденного порта. Бросает RuntimeError, если ничего не найдено.
    """
    ports = candidate_ports if candidate_ports is not None else list_candidate_ports()

    if not ports:
        raise RuntimeError("Не найдено ни одного serial-порта. Проверьте подключение платы реле.")

    print("Поиск платы реле...")
    for port in ports:
        try:
            ser = serial.Serial(port, baudrate=BAUDRATE, timeout=RESPONSE_TIMEOUT)
            time.sleep(BOOT_DELAY)
            ser.reset_input_buffer()
            ser.write(b'BEN\r\n')
            time.sleep(CMD_DELAY)
            raw = ser.read(ser.in_waiting or 1)
            resp = raw.decode('utf-8', errors='ignore').strip()
            ser.close()
            print(f"  {port}  ->  {resp!r}")
            if 'OK' in resp.upper():
                print(f"\nПлата реле найдена на {port}\n")
                return port
        except Exception as e:
            print(f"  {port}  ->  Ошибка при опросе: {e}")

    raise RuntimeError(
        "Не удалось обнаружить плату реле ни на одном порту. "
        "Проверьте подключение или укажите порт вручную (--relay-port)."
    )
