"""
Асинхронный сервис обнаружения приборов (Ф4, п.25/12/11 — архитектура А-4,
PLAN_V2.md).

Отвязан от измерительного цикла: ничего не открывает надолго — каждый скан
опрашивает *IDN? у VISA-ресурса и сразу закрывает соединение (см.
scan_instruments). Держит только последний снимок состояния (DiscoveryState),
UI читает его через periodic-опрос того же рода, каким уже читается очередь
событий измерения (см. gui.py, _drain_events).

ВАЖНО: старт настоящего измерения ВСЕГДА проводит свой полный, независимый
поиск через orchestrate._resolve_instruments/discover_instruments — сервис
только для удобства UI (выпадающие списки, индикатор реле), к моменту клика
«Старт» состояние стенда могло измениться с последнего скана, и открывать
измерение по кэшу вместо свежего опроса — путь к трудноуловимым багам с
устаревшим соединением. Это решение зафиксировано явно в PLAN_V2.md (п.25).

Плата реле проверяется дороже, чем VISA-приборы: рабочий протокол требует
~5 секунд ожидания загрузки после каждого открытия порта (relay.BOOT_DELAY).
Дёргать эту процедуру на каждом тике опроса — значит пересоздавать плате
повод сбоить просто потому, что сервис из любопытства открыл её порт. Поэтому
полная проверка (открыть+BEN) выполняется только когда порт, который сервис
уже опознал как плату реле, либо ещё не был найден, либо пропал из списка
кандидатов (see _scan_relay).
"""
import dataclasses
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from instruments import find_config_for_idn
from relay import discover_relay_port, list_candidate_ports

DEFAULT_POLL_INTERVAL = 5.0


@dataclass(frozen=True)
class DiscoveredInstrument:
    """Один опознанный (или хотя бы откликнувшийся) VISA-ресурс."""
    address: str
    idn: str
    kind: str  # ключ из переданного в scan_instruments config_dirs, либо 'unknown'
    config_path: Optional[Path] = None

    @property
    def label(self) -> str:
        """Текст для выпадающего списка в UI (п.12)."""
        name = self.config_path.stem if self.config_path else "неопознанный прибор"
        return f"{self.address} — {name}"


@dataclass(frozen=True)
class DiscoveryState:
    """Снимок состояния сервиса — то, что читает UI."""
    instruments: List[DiscoveredInstrument] = field(default_factory=list)
    relay_port: Optional[str] = None
    scanning: bool = False
    last_scan_error: Optional[str] = None
    # Сырое число VISA-ресурсов, увиденных на последнем скане (в отличие от
    # instruments — те, что откликнулись на *IDN? и опознались по конфигу).
    # Нужно, чтобы строка статуса NI-VISA в GUI ("ресурсов видно: N") жила,
    # а не застывала на значении со старта программы (баг-репорт: "ресурсов
    # видно: 0", хотя приборы подключены).
    resource_count: Optional[int] = None

    def by_kind(self, kind: str) -> List[DiscoveredInstrument]:
        return [i for i in self.instruments if i.kind == kind]


def scan_instruments(rm, config_dirs: Dict[str, Path], query_timeout: int = 1500) -> List[DiscoveredInstrument]:
    """
    Опрашивает все видимые VISA-ресурсы и сопоставляет каждый ответившим
    IDN с json-конфигами из config_dirs (например {'multimeter': ...,
    'current_source': ..., 'voltage_source': ...} — ключи произвольные,
    попадают напрямую в DiscoveredInstrument.kind).

    Каждый ресурс открывается, опрашивается *IDN? и СРАЗУ закрывается — см.
    модульный docstring, сервис не держит приборы. Ошибка на отдельном
    ресурсе (занят другим процессом, не отвечает) не прерывает скан
    остальных — один сломанный прибор не должен гасить весь список.

    Не совпавший ни с одним каталогом ресурс всё равно попадает в список с
    kind='unknown' — оператору полезно видеть "тут что-то есть, но программа
    не знает что", а не тишину.
    """
    found: List[DiscoveredInstrument] = []
    try:
        resources = rm.list_resources()
    except Exception:
        return found

    for addr in resources:
        try:
            instr = rm.open_resource(addr)
            instr.encoding = 'utf-8'
            instr.timeout = query_timeout
            idn = instr.query('*IDN?').strip()
            instr.close()
        except Exception:
            continue

        matched = False
        for kind, cfg_dir in config_dirs.items():
            if cfg_dir is None:
                continue
            cfg = find_config_for_idn(idn, cfg_dir)
            if cfg is not None:
                found.append(DiscoveredInstrument(address=addr, idn=idn, kind=kind, config_path=cfg))
                matched = True
                break
        if not matched:
            found.append(DiscoveredInstrument(address=addr, idn=idn, kind='unknown'))

    return found


class DiscoveryService:
    """
    Фоновый поток, периодически обновляющий DiscoveryState.

    rm_factory — вызывается на каждом скане, чтобы получить свежий
    pyvisa.ResourceManager (не хранится между сканами намеренно: держать
    открытый ResourceManager между тиками не даёт заметной экономии, а
    свежий на каждый скан проще в рассуждении о его состоянии).
    """

    def __init__(self, rm_factory: Callable[[], object], config_dirs: Dict[str, Path],
                 poll_interval: float = DEFAULT_POLL_INTERVAL,
                 on_update: Optional[Callable[[DiscoveryState], None]] = None,
                 relay_probe: Optional[Callable[[List[str]], str]] = None,
                 port_lister: Callable[[], List[str]] = list_candidate_ports):
        self._rm_factory = rm_factory
        self._config_dirs = config_dirs
        self._poll_interval = poll_interval
        self._on_update = on_update
        self._relay_probe = relay_probe or (lambda ports: discover_relay_port(ports, quiet=True))
        self._port_lister = port_lister

        self._state = DiscoveryState()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._known_relay_port: Optional[str] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def pause(self) -> None:
        """
        Приостанавливает сканирование — вызывается перед стартом измерения
        (п.25: сервис не должен спорить с измерительным циклом за те же
        VISA-ресурсы/serial-порт реле).
        """
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def rescan_now(self) -> DiscoveryState:
        """Форсирует один цикл скана вне очереди (кнопка «Обновить» в UI) и возвращает новый снимок."""
        self._do_scan()
        return self.snapshot()

    def snapshot(self) -> DiscoveryState:
        with self._lock:
            return self._state

    # ------------------------------------------------------------- internals
    def _run(self) -> None:
        while not self._stop_event.is_set():
            if not self._paused.is_set():
                self._do_scan()
            self._stop_event.wait(self._poll_interval)

    def _publish(self, state: DiscoveryState) -> None:
        with self._lock:
            self._state = state
        if self._on_update is not None:
            self._on_update(state)

    def _do_scan(self) -> None:
        self._publish(dataclasses.replace(self.snapshot(), scanning=True))

        try:
            rm = self._rm_factory()
        except Exception as e:
            self._publish(DiscoveryState(scanning=False, last_scan_error=str(e)))
            return

        try:
            try:
                resource_count = len(rm.list_resources())
            except Exception:
                resource_count = None
            instruments = scan_instruments(rm, self._config_dirs)
        finally:
            try:
                rm.close()
            except Exception:
                pass

        relay_port = self._scan_relay()
        self._publish(DiscoveryState(instruments=instruments, relay_port=relay_port, scanning=False,
                                      resource_count=resource_count))

    def _scan_relay(self) -> Optional[str]:
        candidates = self._port_lister()
        # См. модульный docstring: полная проверка (открытие порта + ~5с
        # ожидания загрузки платы) выполняется только когда порт, уже
        # опознанный как плата реле, пропал из списка кандидатов или его
        # ещё не было — иначе сервис доверяет прошлой находке.
        if self._known_relay_port and self._known_relay_port in candidates:
            return self._known_relay_port
        try:
            port = self._relay_probe(candidates)
        except Exception:
            port = None
        self._known_relay_port = port
        return port
