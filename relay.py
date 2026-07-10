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
BOOT_DELAY = 5.0       # время на загрузку контроллера после открытия порта
CMD_DELAY = 0.3        # начальная пауза после отправки команды перед первым чтением
RESPONSE_TIMEOUT = 1.0
READ_RETRY_DELAY = 0.1   # пауза между повторными попытками дочитать буфер
READ_MAX_WAIT = 1.5      # суммарное время ожидания ответа на команду


def _read_response(ser: serial.Serial, max_wait: float = READ_MAX_WAIT) -> str:
    """
    Плата может присылать ответ несколькими пакетами с паузой между ними
    (например эхо команды сразу, а 'OK' — заметно позже), поэтому один снимок
    in_waiting сразу после короткой фиксированной паузы читает только часть
    ответа и оставляет "хвост" в буфере до следующей команды — это и
    проявлялось как склейка вида 'OKтвет реле: RW' в выводе.

    Поэтому здесь просто копим байты до max_wait или до появления 'OK' в
    накопленном ответе (плюс небольшой запас на возможный завершающий \r\n).
    Никакого early-exit по "долго было тихо" — пауза между пакетами платы
    оказалась больше, чем можно было безопасно принять за конец ответа.
    """
    buf = b''
    waited = 0.0
    while waited < max_wait:
        time.sleep(READ_RETRY_DELAY)
        waited += READ_RETRY_DELAY
        n = ser.in_waiting
        if n:
            buf += ser.read(n)
            if b'OK' in buf.upper():
                time.sleep(READ_RETRY_DELAY)
                if ser.in_waiting:
                    buf += ser.read(ser.in_waiting)
                break
    return buf.decode('utf-8', errors='ignore').strip()


class RelayController:
    """Обёртка над платой релейного переключателя направления тока."""

    def __init__(self, port: str, wait_for_boot: bool = True):
        self.port = port
        self.ser = serial.Serial(port, baudrate=BAUDRATE, timeout=RESPONSE_TIMEOUT)
        if wait_for_boot:
            time.sleep(BOOT_DELAY)
        self.ser.reset_input_buffer()

    def _send(self, cmd: str) -> str:
        self.ser.reset_input_buffer()
        self.ser.write(cmd.encode('utf-8') + b'\r\n')
        return _read_response(self.ser)

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
            resp = _read_response(ser)
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
