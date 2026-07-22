# Headless backend — тесты не должны открывать окна графиков и должны
# работать в CI/без дисплея. Должно быть установлено до первого импорта
# analysis.py (который импортирует matplotlib.pyplot на уровне модуля).
import matplotlib
matplotlib.use("Agg")

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTRUMENTS_DIR = REPO_ROOT / "instruments"


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def instruments_dir() -> Path:
    return INSTRUMENTS_DIR


# ----------------------------------------------------------------------
# Фейковые объекты PyVISA — заменяют реальный pyvisa.ResourceManager /
# instrument, чтобы instruments.py можно было тестировать без физических
# приборов и без установленного бэкенда NI-VISA.
# ----------------------------------------------------------------------

class FakeVisaResource:
    """
    Имитирует объект прибора, возвращаемый pyvisa.ResourceManager.open_resource().

    idn: если задан, возвращается на query('*IDN?').
    query_responses: очередь ответов на прочие query(); каждый элемент —
    либо строка-ответ, либо инстанс исключения (тогда он поднимается).
    """

    def __init__(self, idn: str = None, query_responses=None):
        self.idn = idn
        self.query_responses = list(query_responses or [])
        self.written = []
        self.queried = []
        self.encoding = None
        self.timeout = None
        self.closed = False

    def write(self, cmd: str):
        self.written.append(cmd)

    def query(self, cmd: str) -> str:
        self.queried.append(cmd)
        if cmd.strip() == '*IDN?' and self.idn is not None:
            return self.idn
        if not self.query_responses:
            raise AssertionError(f"FakeVisaResource: нет заготовленного ответа на query({cmd!r})")
        resp = self.query_responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def close(self):
        self.closed = True


class FakeResourceManager:
    """Имитирует pyvisa.ResourceManager для discover_instruments()/прямого открытия ресурса."""

    def __init__(self, resources: dict = None):
        # resources: {addr: FakeVisaResource}
        self._resources = dict(resources or {})

    def list_resources(self):
        return tuple(self._resources.keys())

    def open_resource(self, addr: str) -> FakeVisaResource:
        return self._resources[addr]


@pytest.fixture
def make_fake_rm():
    def _make(resources: dict) -> FakeResourceManager:
        return FakeResourceManager(resources)
    return _make


# ----------------------------------------------------------------------
# Фейковый serial.Serial — для тестов relay.py без реальной платы.
# ----------------------------------------------------------------------

class FakeSerial:
    """
    chunks: список порций байт, каждая "приходит" (становится видна через
    in_waiting) только после того, как предыдущая порция полностью
    вычитана — так можно смоделировать ответ платы несколькими пакетами
    с паузой (историческая ошибка склейки ответов, см. relay.py).
    """

    def __init__(self, chunks=None):
        self._chunks = list(chunks or [])
        self._buffer = b''
        self.written = []
        self.closed = False
        self.reset_calls = 0

    def reset_input_buffer(self):
        self._buffer = b''
        self.reset_calls += 1

    def write(self, data: bytes):
        self.written.append(data)

    @property
    def in_waiting(self) -> int:
        if not self._buffer and self._chunks:
            self._buffer = self._chunks.pop(0)
        return len(self._buffer)

    def read(self, n: int) -> bytes:
        data = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return data

    def close(self):
        self.closed = True


@pytest.fixture
def fake_serial_factory():
    """Фабрика FakeSerial с заданными чанками ответа."""
    def _make(chunks=None):
        return FakeSerial(chunks=chunks)
    return _make
