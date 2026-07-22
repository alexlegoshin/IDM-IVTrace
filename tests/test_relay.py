import pytest

import relay
from relay import RelayController, _read_response, discover_relay_port, list_candidate_ports


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    """Убирает реальные задержки (BOOT_DELAY, READ_RETRY_DELAY) из тестов."""
    monkeypatch.setattr(relay.time, "sleep", lambda s: None)


def test_read_response_single_packet(fake_serial_factory):
    ser = fake_serial_factory(chunks=[b'OK\r\n'])
    resp = _read_response(ser)
    assert 'OK' in resp.upper()


def test_read_response_accumulates_split_packets(fake_serial_factory):
    """
    Регрессия: плата может прислать эхо команды отдельным пакетом, а 'OK' —
    заметно позже вторым пакетом. Старая реализация (снимок in_waiting сразу
    после фиксированной паузы) читала только первый пакет и путала хвост со
    следующим ответом. _read_response должен накапливать байты до появления
    'OK' в буфере целиком.
    """
    ser = fake_serial_factory(chunks=[b'IFW\r\n', b'OK\r\n'])
    resp = _read_response(ser)
    assert 'IFW' in resp
    assert 'OK' in resp.upper()
    # буфер вычитан полностью, ничего не осталось "на следующую команду"
    assert ser.in_waiting == 0


def test_read_response_times_out_without_ok(fake_serial_factory):
    ser = fake_serial_factory(chunks=[b'ERR\r\n'])
    resp = _read_response(ser, max_wait=0.05)
    assert 'ERR' in resp
    assert 'OK' not in resp.upper()


def test_relay_controller_sends_correct_commands(monkeypatch, fake_serial_factory):
    ser = fake_serial_factory(chunks=[b'OK\r\n'])
    monkeypatch.setattr(relay.serial, "Serial", lambda *a, **kw: ser)

    controller = RelayController("COM_FAKE")

    resp = controller.forward()
    assert ser.written[-1] == b'IFW\r\n'
    assert 'OK' in resp.upper()

    ser._chunks = [b'OK\r\n']
    resp = controller.reverse()
    assert ser.written[-1] == b'IRW\r\n'
    assert 'OK' in resp.upper()

    ser._chunks = [b'OK\r\n']
    resp = controller.off()
    assert ser.written[-1] == b'I_0\r\n'
    assert 'OK' in resp.upper()

    ser._chunks = [b'OK\r\n']
    resp = controller.check()
    assert ser.written[-1] == b'BEN\r\n'
    assert 'OK' in resp.upper()


def test_relay_controller_close_sends_off_and_closes_port(monkeypatch, fake_serial_factory):
    ser = fake_serial_factory(chunks=[b'OK\r\n'] * 5)
    monkeypatch.setattr(relay.serial, "Serial", lambda *a, **kw: ser)

    controller = RelayController("COM_FAKE")
    controller.close()

    assert b'I_0\r\n' in ser.written
    assert ser.closed is True


def test_relay_controller_close_is_safe_if_off_fails(monkeypatch, fake_serial_factory):
    """close() не должен падать, даже если port уже недоступен."""
    ser = fake_serial_factory(chunks=[b'OK\r\n'])
    monkeypatch.setattr(relay.serial, "Serial", lambda *a, **kw: ser)
    controller = RelayController("COM_FAKE")

    def boom(cmd):
        raise RuntimeError("port gone")
    monkeypatch.setattr(controller, "_send", boom)

    controller.close()  # не должно бросить исключение


def test_discover_relay_port_finds_correct_port(monkeypatch):
    responses = {
        "COM1": b'garbage\r\n',
        "COM2": b'OK\r\n',
    }

    def fake_serial_ctor(port, baudrate, timeout):
        return type("S", (), {
            "reset_input_buffer": lambda self: None,
            "write": lambda self, data: None,
            "in_waiting": len(responses[port]),
            "read": lambda self, n, _p=port: responses[_p][:n],
            "close": lambda self: None,
        })()

    monkeypatch.setattr(relay.serial, "Serial", fake_serial_ctor)

    port = discover_relay_port(candidate_ports=["COM1", "COM2"])
    assert port == "COM2"


def test_discover_relay_port_raises_when_nothing_found(monkeypatch):
    def fake_serial_ctor(port, baudrate, timeout):
        return type("S", (), {
            "reset_input_buffer": lambda self: None,
            "write": lambda self, data: None,
            "in_waiting": 0,
            "read": lambda self, n: b'',
            "close": lambda self: None,
        })()

    monkeypatch.setattr(relay.serial, "Serial", fake_serial_ctor)

    with pytest.raises(RuntimeError):
        discover_relay_port(candidate_ports=["COM1"])


def test_discover_relay_port_raises_when_no_ports_available():
    with pytest.raises(RuntimeError):
        discover_relay_port(candidate_ports=[])


def test_list_candidate_ports_returns_list():
    # Не проверяем содержимое (зависит от машины), только что не падает
    # и возвращает список.
    assert isinstance(list_candidate_ports(), list)
