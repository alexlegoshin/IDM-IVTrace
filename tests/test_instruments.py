import json

import pytest

from instruments import (
    Multimeter, CurrentSource, VoltageSource,
    find_config_for_idn, discover_instruments,
)
from tests.conftest import FakeVisaResource, FakeResourceManager


# ----------------------------------------------------------------------
# find_config_for_idn
# ----------------------------------------------------------------------

def test_find_config_for_idn_matches_by_keyword(instruments_dir):
    cfg = find_config_for_idn("Instrument reply: SIGLENT,SDM3055,...", instruments_dir / "multimeters")
    assert cfg is not None
    assert cfg.name == "akip2101.json"


def test_find_config_for_idn_case_insensitive(instruments_dir):
    cfg = find_config_for_idn("picotest model v7-78/1", instruments_dir / "multimeters")
    assert cfg is not None
    assert cfg.name == "akipb778.json"


def test_find_config_for_idn_no_match_returns_none(instruments_dir):
    cfg = find_config_for_idn("SOME,UNRELATED,DEVICE", instruments_dir / "multimeters")
    assert cfg is None


def test_gpp_idn_missing_leading_digit_still_matches(instruments_dir):
    # Задокументированный в README нюанс: GPP-74323 отвечает как "GPP-4323".
    cfg = find_config_for_idn("GW,GPP-4323,SN123,1.0", instruments_dir / "voltage_sources")
    assert cfg is not None
    assert cfg.name == "gpp74323.json"


# ----------------------------------------------------------------------
# Multimeter — регрессия на баг с MEAS?/READ? и авто-диапазоном
# ----------------------------------------------------------------------

@pytest.fixture
def akip2101_cfg(instruments_dir):
    return instruments_dir / "multimeters" / "akip2101.json"


def test_multimeter_init_sends_init_commands_and_max_range(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    cfg = json.loads(akip2101_cfg.read_text(encoding='utf-8'))
    for cmd in cfg['init_commands']:
        assert cmd in fake.written

    # Стартовый диапазон — максимальный.
    max_range = cfg['ranges'][-1]
    assert dmm.current_range_idx == len(cfg['ranges']) - 1
    assert fake.written[-1] == f'SENS:CURR:DC:RANG {max_range}'


def test_multimeter_measure_command_is_read_not_meas(akip2101_cfg):
    """
    Регрессия: MEAS:CURR:DC?/CONF? по SCPI сбрасывают диапазон обратно в
    AUTO при каждом вызове, из-за чего ручной auto_range()/set_range()
    переставали иметь эффект. Конфиг обязан использовать READ?/FETC?.
    """
    cfg = json.loads(akip2101_cfg.read_text(encoding='utf-8'))
    cmd = cfg['measure_command'].upper()
    assert not cmd.startswith('MEAS'), "MEAS? сбрасывает диапазон прибора в AUTO при каждом чтении"
    assert not cmd.startswith('CONF'), "CONF? сбрасывает диапазон прибора в AUTO при каждом чтении"


def test_multimeter_measure_current_uses_configured_command(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource(query_responses=["0.001234"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    value = dmm.measure_current()

    assert value == pytest.approx(0.001234)
    assert fake.queried[-1] == "READ?"


def test_auto_range_is_first_picks_smallest_covering_range(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.auto_range(0.015, is_first=True)
    assert dmm.ranges[dmm.current_range_idx] == 0.02
    assert fake.written[-1] == 'SENS:CURR:DC:RANG 0.02'


def test_auto_range_is_first_falls_back_to_max_when_over_range(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.auto_range(999.0, is_first=True)
    assert dmm.current_range_idx == len(dmm.ranges) - 1


def test_auto_range_steps_up_above_95_percent(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.current_range_idx = 2  # range 0.02
    dmm.auto_range(0.0195, is_first=False)  # > 95% of 0.02
    assert dmm.current_range_idx == 3
    assert fake.written[-1] == f'SENS:CURR:DC:RANG {dmm.ranges[3]}'


def test_auto_range_steps_down_below_10_percent(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.current_range_idx = 3  # range 0.2
    dmm.auto_range(0.001, is_first=False)  # < 10% of 0.2 -> шаг вниз на один диапазон (0.02), не к минимально достаточному
    assert dmm.ranges[dmm.current_range_idx] == 0.02


def test_auto_range_stays_put_within_normal_band(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.current_range_idx = 3  # range 0.2
    before = dmm.current_range_idx
    dmm.auto_range(0.05, is_first=False)  # between 10% and 95% of 0.2
    assert dmm.current_range_idx == before


def test_multimeter_close_does_not_raise(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    dmm.close()
    assert fake.closed is True


# ----------------------------------------------------------------------
# CurrentSource / VoltageSource
# ----------------------------------------------------------------------

def test_current_source_setup_sends_voltage_limit_and_zero_current(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "current_sources" / "akip1162.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", cfg_path, rm=rm)
    fake.written.clear()
    src.setup(voltage_limit=5.0)

    assert "SOUR:VOLT 5.0" in fake.written
    assert "SOUR:CURR 0" in fake.written


def test_current_source_set_current_and_output(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "current_sources" / "akip1162.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", cfg_path, rm=rm)
    src.set_current(2.5)
    src.output_on()
    src.output_off()

    assert "SOUR:CURR 2.5" in fake.written
    assert "OUTP ON" in fake.written
    assert "OUTP OFF" in fake.written


def test_current_source_shutdown_zeroes_and_turns_off(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "current_sources" / "akip1162.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = CurrentSource("A", cfg_path, rm=rm)
    fake.written.clear()
    src.shutdown()

    assert fake.written == ["SOUR:CURR 0", "OUTP OFF"]


def test_voltage_source_init_enables_tracking_series(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "voltage_sources" / "gpp74323.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = VoltageSource("A", cfg_path, rm=rm)
    assert fake.written[-1] == "TRACK1"
    assert src.primary_ch == 1


def test_voltage_source_set_voltage_uses_primary_channel(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "voltage_sources" / "gpp74323.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = VoltageSource("A", cfg_path, rm=rm)
    src.set_voltage(12.5)
    assert fake.written[-1] == "VSET1:12.5"


def test_voltage_source_output_on_off_uses_primary_channel(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "voltage_sources" / "gpp74323.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = VoltageSource("A", cfg_path, rm=rm)
    src.output_on()
    src.output_off()
    assert ":OUTPut1:STATe ON" in fake.written
    assert ":OUTPut1:STATe OFF" in fake.written


# ----------------------------------------------------------------------
# discover_instruments
# ----------------------------------------------------------------------

def test_discover_instruments_finds_dmm_and_source(instruments_dir):
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    src_res = FakeVisaResource(idn="ITECH,IT-M3122,SN002,1.0")
    rm = FakeResourceManager({"DMM_ADDR": dmm_res, "SRC_ADDR": src_res})

    dmm_addr, dmm_cfg, src_addr, src_cfg = discover_instruments(
        instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm,
    )

    assert dmm_addr == "DMM_ADDR"
    assert dmm_cfg.name == "akip2101.json"
    assert src_addr == "SRC_ADDR"
    assert src_cfg.name == "akip1162.json"


def test_discover_instruments_raises_when_no_resources(instruments_dir):
    from tests.conftest import FakeResourceManager
    rm = FakeResourceManager({})
    with pytest.raises(RuntimeError):
        discover_instruments(instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm)


def test_discover_instruments_raises_when_source_missing(instruments_dir):
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    rm = FakeResourceManager({"DMM_ADDR": dmm_res})

    with pytest.raises(RuntimeError, match="источник"):
        discover_instruments(instruments_dir / "multimeters", instruments_dir / "current_sources", rm=rm)
