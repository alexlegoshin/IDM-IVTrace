import time

import pytest

from discovery import DiscoveredInstrument, DiscoveryState, DiscoveryService, scan_instruments
from tests.conftest import FakeVisaResource, FakeResourceManager


# ----------------------------------------------------------------------
# scan_instruments
# ----------------------------------------------------------------------

def test_scan_instruments_matches_by_config_dir(instruments_dir):
    rm = FakeResourceManager({
        "DMM": FakeVisaResource(idn="AKIP-2101"),
        "SRC": FakeVisaResource(idn="ITECH IT-M3130"),
    })
    config_dirs = {
        'multimeter': instruments_dir / "multimeters_current",
        'current_source': instruments_dir / "current_sources",
    }
    found = scan_instruments(rm, config_dirs)

    by_addr = {i.address: i for i in found}
    assert by_addr["DMM"].kind == 'multimeter'
    assert by_addr["DMM"].config_path.parent == config_dirs['multimeter']
    assert by_addr["SRC"].kind == 'current_source'


def test_scan_instruments_tags_unmatched_as_unknown(instruments_dir):
    rm = FakeResourceManager({"MYSTERY": FakeVisaResource(idn="NOBODY KNOWS THIS MODEL")})
    found = scan_instruments(rm, {'multimeter': instruments_dir / "multimeters_current"})
    assert len(found) == 1
    assert found[0].kind == 'unknown'
    assert found[0].config_path is None


def test_scan_instruments_closes_every_opened_resource(instruments_dir):
    dmm_res = FakeVisaResource(idn="AKIP-2101")
    rm = FakeResourceManager({"DMM": dmm_res})
    scan_instruments(rm, {'multimeter': instruments_dir / "multimeters_current"})
    assert dmm_res.closed is True


def test_scan_instruments_skips_resource_that_raises_on_query(instruments_dir):
    class DyingResource(FakeVisaResource):
        def query(self, cmd):
            raise RuntimeError("прибор занят другим процессом")

    rm = FakeResourceManager({
        "BROKEN": DyingResource(),
        "OK": FakeVisaResource(idn="AKIP-2101"),
    })
    found = scan_instruments(rm, {'multimeter': instruments_dir / "multimeters_current"})
    assert len(found) == 1
    assert found[0].address == "OK"


def test_scan_instruments_returns_empty_list_when_list_resources_fails():
    class BrokenRM:
        def list_resources(self):
            raise RuntimeError("VISA backend недоступен")

    assert scan_instruments(BrokenRM(), {}) == []


def test_scan_instruments_returns_empty_list_for_no_resources():
    assert scan_instruments(FakeResourceManager({}), {}) == []


# ----------------------------------------------------------------------
# DiscoveredInstrument.label
# ----------------------------------------------------------------------

def test_label_shows_config_name_when_matched(instruments_dir):
    cfg = instruments_dir / "multimeters_current" / "akip2101.json"
    instr = DiscoveredInstrument(address="ADDR1", idn="AKIP-2101", kind='multimeter', config_path=cfg)
    assert instr.label == "ADDR1 — akip2101"


def test_label_shows_placeholder_when_unmatched():
    instr = DiscoveredInstrument(address="ADDR1", idn="???", kind='unknown')
    assert "неопознанный" in instr.label


# ----------------------------------------------------------------------
# DiscoveryState.by_kind
# ----------------------------------------------------------------------

def test_by_kind_filters_instruments():
    state = DiscoveryState(instruments=[
        DiscoveredInstrument(address="A", idn="x", kind='multimeter'),
        DiscoveredInstrument(address="B", idn="y", kind='current_source'),
    ])
    assert [i.address for i in state.by_kind('multimeter')] == ["A"]
    assert [i.address for i in state.by_kind('current_source')] == ["B"]
    assert state.by_kind('voltage_source') == []


# ----------------------------------------------------------------------
# DiscoveryService — скан, публикация состояния, кэш реле
# ----------------------------------------------------------------------

def _make_rm_factory(resources):
    def factory():
        return FakeResourceManager(resources)
    return factory


def test_rescan_now_populates_state(instruments_dir):
    rm_factory = _make_rm_factory({"DMM": FakeVisaResource(idn="AKIP-2101")})
    svc = DiscoveryService(
        rm_factory, {'multimeter': instruments_dir / "multimeters_current"},
        relay_probe=lambda ports: (_ for _ in ()).throw(RuntimeError("нет платы")),
        port_lister=lambda: [],
    )
    state = svc.rescan_now()
    assert len(state.instruments) == 1
    assert state.instruments[0].address == "DMM"
    assert state.relay_port is None
    assert state.scanning is False


def test_rescan_now_finds_relay_port(instruments_dir):
    rm_factory = _make_rm_factory({})
    svc = DiscoveryService(
        rm_factory, {},
        relay_probe=lambda ports: "COM7",
        port_lister=lambda: ["COM7"],
    )
    state = svc.rescan_now()
    assert state.relay_port == "COM7"


def test_relay_probe_not_called_again_while_known_port_still_listed():
    calls = []

    def probe(ports):
        calls.append(ports)
        return "COM3"

    rm_factory = _make_rm_factory({})
    svc = DiscoveryService(rm_factory, {}, relay_probe=probe, port_lister=lambda: ["COM3"])

    svc.rescan_now()
    svc.rescan_now()
    svc.rescan_now()

    assert len(calls) == 1  # второй и третий раз просто доверились кэшу


def test_relay_probe_runs_again_after_known_port_disappears():
    calls = []

    def probe(ports):
        calls.append(list(ports))
        return "COM3"

    rm_factory = _make_rm_factory({})
    ports_now = ["COM3"]
    svc = DiscoveryService(rm_factory, {}, relay_probe=probe, port_lister=lambda: ports_now)

    svc.rescan_now()
    assert len(calls) == 1

    ports_now.clear()  # плату отключили
    svc.rescan_now()
    assert len(calls) == 2


def test_scan_error_is_captured_not_raised():
    def broken_factory():
        raise RuntimeError("NI-VISA не установлена")

    svc = DiscoveryService(broken_factory, {}, port_lister=lambda: [])
    state = svc.rescan_now()
    assert state.last_scan_error == "NI-VISA не установлена"
    assert state.instruments == []


def test_on_update_callback_receives_each_published_state():
    updates = []
    rm_factory = _make_rm_factory({})
    svc = DiscoveryService(
        rm_factory, {}, on_update=updates.append,
        relay_probe=lambda ports: None, port_lister=lambda: [],
    )
    svc.rescan_now()
    # Минимум два вызова: "scanning=True" сразу, затем финальное состояние.
    assert len(updates) >= 2
    assert updates[-1].scanning is False


# ----------------------------------------------------------------------
# DiscoveryService — фоновый поток: старт/пауза/стоп не подвисают
# ----------------------------------------------------------------------

def test_background_thread_scans_and_can_be_stopped():
    scan_count = {'n': 0}

    def rm_factory():
        scan_count['n'] += 1
        return FakeResourceManager({})

    svc = DiscoveryService(
        rm_factory, {}, poll_interval=0.02,
        relay_probe=lambda ports: None, port_lister=lambda: [],
    )
    svc.start()
    try:
        deadline = time.time() + 2.0
        while scan_count['n'] < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert scan_count['n'] >= 2
    finally:
        svc.stop()

    count_after_stop = scan_count['n']
    time.sleep(0.1)
    assert scan_count['n'] == count_after_stop  # поток реально остановлен


def test_pause_prevents_scanning_until_resumed():
    scan_count = {'n': 0}

    def rm_factory():
        scan_count['n'] += 1
        return FakeResourceManager({})

    svc = DiscoveryService(
        rm_factory, {}, poll_interval=0.02,
        relay_probe=lambda ports: None, port_lister=lambda: [],
    )
    svc.pause()
    svc.start()
    try:
        time.sleep(0.1)
        assert scan_count['n'] == 0  # ни одного скана, пока на паузе

        svc.resume()
        deadline = time.time() + 2.0
        while scan_count['n'] < 1 and time.time() < deadline:
            time.sleep(0.01)
        assert scan_count['n'] >= 1
    finally:
        svc.stop()


def test_start_is_idempotent():
    svc = DiscoveryService(
        _make_rm_factory({}), {}, poll_interval=1.0,
        relay_probe=lambda ports: None, port_lister=lambda: [],
    )
    svc.start()
    thread1 = svc._thread
    svc.start()  # второй вызов не должен создать второй поток
    assert svc._thread is thread1
    svc.stop()
