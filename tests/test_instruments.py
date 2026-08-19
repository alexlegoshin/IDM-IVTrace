import json

import pytest

from instruments import (
    Multimeter, CurrentSource, VoltageSource,
    find_config_for_idn, discover_instruments, is_overflow_reading, parse_scpi_number,
    identify_instrument, relay_visa_address_match,
)
from tests.conftest import FakeVisaResource, FakeResourceManager


# ----------------------------------------------------------------------
# find_config_for_idn
# ----------------------------------------------------------------------

def test_find_config_for_idn_matches_by_keyword(instruments_dir):
    cfg = find_config_for_idn("Instrument reply: SIGLENT,SDM3055,...", instruments_dir / "multimeters_current")
    assert cfg is not None
    assert cfg.name == "akip2101.json"


def test_find_config_for_idn_case_insensitive(instruments_dir):
    cfg = find_config_for_idn("picotest model v7-78/1", instruments_dir / "multimeters_current")
    assert cfg is not None
    assert cfg.name == "akipb778.json"


def test_find_config_for_idn_no_match_returns_none(instruments_dir):
    cfg = find_config_for_idn("SOME,UNRELATED,DEVICE", instruments_dir / "multimeters_current")
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
    return instruments_dir / "multimeters_current" / "akip2101.json"


@pytest.fixture
def akip2101_manual_range_cfg(instruments_dir, tmp_path):
    """
    Ручной диапазон (manual_range) в конфиге АКИП-2101 по умолчанию
    выключен (прибор остаётся на встроенном авто-диапазоне, см.
    instruments.py). Тесты ниже целенаправленно проверяют ручное
    управление диапазоном (set_range/auto_range), поэтому включают его
    явно поверх реального конфига, во временном файле.
    """
    cfg = json.loads((instruments_dir / "multimeters_current" / "akip2101.json").read_text(encoding='utf-8'))
    cfg['manual_range'] = True
    # По умолчанию set_range() спит DEFAULT_RANGE_SETTLE_DELAY (0.7 с) после
    # каждой смены диапазона — это сознательная задержка на устаканивание
    # прибора (см. instruments.py), а не то, что должно тормозить тесты
    # логики переключения. Сама задержка проверяется отдельным тестом.
    cfg['range_settle_delay'] = 0
    path = tmp_path / "akip2101_manual.json"
    path.write_text(json.dumps(cfg), encoding='utf-8')
    return path


def test_go_local_sends_configured_command(akip2101_cfg, make_fake_rm):
    """п.8: go_local() шлёт local_command (в конфиге АКИП-2101 — 'SYST:LOC')."""
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    fake.written.clear()
    dmm.go_local()
    assert "SYST:LOC" in fake.written


def test_go_local_noop_without_command(akip2101_cfg, make_fake_rm, tmp_path):
    """Без local_command в конфиге go_local() ничего не шлёт и не падает."""
    cfg = json.loads(akip2101_cfg.read_text(encoding="utf-8"))
    cfg.pop("local_command", None)
    path = tmp_path / "no_local.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", path, rm=rm)
    fake.written.clear()
    dmm.go_local()
    assert fake.written == []


@pytest.mark.parametrize("visa_addr,relay_port,expected", [
    ("ASRL3::INSTR", "COM3", True),          # Windows: COM3 <-> ASRL3
    ("ASRL3::INSTR", "COM4", False),         # другой номер — не реле
    ("USB0::0x1234::0x5678::INSTR", "COM3", False),  # USB-TMC прибор — не реле
    ("ASRL/dev/ttyUSB0::INSTR", "/dev/ttyUSB0", True),  # Unix: по подстроке
    ("ASRL1::INSTR", None, False),           # порт реле неизвестен — не исключаем
])
def test_relay_visa_address_match(visa_addr, relay_port, expected):
    assert relay_visa_address_match(visa_addr, relay_port) is expected


def test_multimeter_uses_autorange_by_default(akip2101_cfg, make_fake_rm):
    """
    manual_range по умолчанию выключен (отсутствует в конфиге) — прибор
    остаётся на встроенном авто-диапазоне, ни disable_autorange_command,
    ни range_command не отправляются.
    """
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    dmm.auto_range(0.015, is_first=True)

    assert not any("RANG" in cmd for cmd in fake.written)


def test_multimeter_init_sends_init_commands_and_max_range(akip2101_manual_range_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", akip2101_manual_range_cfg, rm=rm)

    cfg = json.loads(akip2101_manual_range_cfg.read_text(encoding='utf-8'))
    for cmd in cfg['init_commands']:
        assert cmd in fake.written
    assert cfg['disable_autorange_command'] in fake.written

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


def test_multimeter_measure_and_measure_current_are_the_same_call(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource(query_responses=["0.5", "0.5"])
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    assert dmm.measure() == pytest.approx(0.5)
    assert dmm.measure_current() == pytest.approx(0.5)


def test_multimeter_measure_voltage_is_an_alias_for_measure(instruments_dir, make_fake_rm):
    """
    Ф1 п.4: measure_voltage() — тот же самый запрос, что measure(), просто
    семантически понятное имя, когда прибор явно настроен как вольтметр.
    Используется здесь конфиг АКИП-2101 из multimeters_voltage/ (см. п.4).
    """
    cfg_path = instruments_dir / "multimeters_voltage" / "akip2101.json"
    fake = FakeVisaResource(query_responses=["12.345"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", cfg_path, rm=rm)
    value = dmm.measure_voltage()

    assert value == pytest.approx(12.345)
    assert fake.queried[-1] == "MEAS:VOLT:DC?"


# ----------------------------------------------------------------------
# write_termination/read_termination (Ф1 п.4 — RIGOL DM3068 voltmeter)
# ----------------------------------------------------------------------

def test_multimeter_applies_configured_terminations(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "multimeters_voltage" / "rigol_dm3068.json"
    fake = FakeVisaResource(query_responses=[])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    Multimeter("FAKE::ADDR", cfg_path, rm=rm)

    assert fake.write_termination == '\n'
    assert fake.read_termination == '\n'


def test_multimeter_without_termination_fields_leaves_visa_defaults(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})

    Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    assert not hasattr(fake, 'write_termination')
    assert not hasattr(fake, 'read_termination')


# ----------------------------------------------------------------------
# multimeters_voltage/*.json — работают как реальные конфиги (Ф1 п.4)
# ----------------------------------------------------------------------

def test_voltmeter_rigol_dm3068_index_style_range_command(instruments_dir, tmp_path, make_fake_rm):
    """Конфиг из репозитория, а не синтетика — тот самый {index}-стиль, проверенный в IDM-DNKMetr."""
    cfg = json.loads((instruments_dir / "multimeters_voltage" / "rigol_dm3068.json").read_text(encoding='utf-8'))
    cfg['manual_range'] = True  # по умолчанию выключен; здесь целенаправленно проверяем сам стиль команды
    cfg['range_settle_delay'] = 0
    path = tmp_path / "rigol_voltmeter_manual.json"
    path.write_text(json.dumps(cfg), encoding='utf-8')

    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", path, rm=rm)

    dmm.current_range_idx = 3
    dmm.set_range(dmm.ranges[3])

    assert fake.written[-1] == ':MEASure:VOLTage:DC 3'


def test_voltmeter_akip2101_measures_voltage_via_meas_command(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "multimeters_voltage" / "akip2101.json"
    fake = FakeVisaResource(query_responses=["3.3000"])
    rm = make_fake_rm({"FAKE::ADDR": fake})

    dmm = Multimeter("FAKE::ADDR", cfg_path, rm=rm)

    assert dmm.measure_voltage() == pytest.approx(3.3)
    assert fake.queried[-1] == "MEAS:VOLT:DC?"


def test_voltmeter_akipb778_ranges_match_agilent_34401a_platform(instruments_dir):
    """
    Регрессия конкретного факта из IDM-DTCal: шкалы DCV у этого прибора
    0.1/1/10/100/1000 В (платформа Agilent 34401A), НЕ 0.2/2/20/200/1000
    как у Siglent/Rigol — раньше эта путаница резала разрешение измерения.
    """
    cfg = json.loads((instruments_dir / "multimeters_voltage" / "akipb778.json").read_text(encoding='utf-8'))
    assert cfg['ranges'] == [0.1, 1.0, 10.0, 100.0, 1000.0]


def test_auto_range_is_first_picks_smallest_covering_range(akip2101_manual_range_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_manual_range_cfg, rm=rm)

    dmm.auto_range(0.015, is_first=True)
    assert dmm.ranges[dmm.current_range_idx] == 0.02
    assert fake.written[-1] == 'SENS:CURR:DC:RANG 0.02'


def test_auto_range_is_first_falls_back_to_max_when_over_range(akip2101_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.auto_range(999.0, is_first=True)
    assert dmm.current_range_idx == len(dmm.ranges) - 1


def test_auto_range_steps_up_above_95_percent(akip2101_manual_range_cfg, make_fake_rm):
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_manual_range_cfg, rm=rm)

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


# ----------------------------------------------------------------------
# auto_range — гистерезис не должен провоцировать автоколебания (Ф1 п.6)
# ----------------------------------------------------------------------

def test_auto_range_step_down_does_not_land_at_the_upper_edge_of_new_range(akip2101_cfg, make_fake_rm):
    """
    Регрессия на автоколебания: старый порог спуска (<10% ТЕКУЩЕГО
    диапазона) на декадных шкалах совпадал со 100% диапазона на ступень
    ниже — сразу после спуска значение оказывалось на самой верхней границе
    нового диапазона и тут же провоцировало обратный подъём.

    ranges = [0.0002, 0.002, 0.02, 0.2, 2.0, 10.0]. current_range_idx=3
    (0.2), значение 0.0195 — это чуть МЕНЬШЕ старого порога спуска (0.02),
    но 97.5% диапазона на ступень ниже (0.02). Старый алгоритм спустился бы
    сюда и тут же снова поднялся бы на следующем вызове. Новый — не должен
    спускаться вовсе, раз значение не уместится с запасом в диапазон ниже.
    """
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.current_range_idx = 3  # range 0.2
    dmm.auto_range(0.0195, is_first=False)

    assert dmm.current_range_idx == 3, "не должен был спуститься — 0.0195 не влезает с запасом в 0.02"


def test_auto_range_step_down_lands_comfortably_below_the_up_threshold(akip2101_cfg, make_fake_rm):
    """
    Когда спуск всё же происходит, новое показание должно оказаться далеко
    от порога обратного подъёма — иначе шум на границе снова закачает
    диапазон туда-сюда.
    """
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)

    dmm.current_range_idx = 3  # range 0.2
    dmm.auto_range(0.001, is_first=False)  # 5% диапазона ступенью ниже (0.02)

    assert dmm.current_range_idx == 2
    new_range = dmm.ranges[dmm.current_range_idx]
    assert 0.001 <= new_range * 0.95, "приземлились слишком близко к порогу обратного подъёма"


def test_auto_range_never_oscillates_around_a_decade_boundary(akip2101_cfg, make_fake_rm):
    """
    Прогоняем auto_range() многократно на значении ровно у старой границы
    (10% текущего диапазона = 100% диапазона ниже) — диапазон должен
    зафиксироваться и больше не переключаться, а не бегать туда-сюда.
    """
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    dmm.current_range_idx = 3  # range 0.2

    boundary_value = 0.02  # старая граница переключения
    seen_indices = set()
    for _ in range(10):
        dmm.auto_range(boundary_value, is_first=False)
        seen_indices.add(dmm.current_range_idx)

    assert len(seen_indices) == 1, f"диапазон переключался туда-сюда: {seen_indices}"


# ----------------------------------------------------------------------
# auto_range — подъём на большой единичный скачок (issue #3, Ф1 п.6)
# ----------------------------------------------------------------------

def test_auto_range_is_first_jumps_directly_across_multiple_decades(akip2101_manual_range_cfg, make_fake_rm):
    """
    is_first=True (в т.ч. предсказание диапазона по X_set/ratio, см.
    measurement.py) обязано прыгать сразу на нужный диапазон, а не
    подниматься по одной ступени — именно так теперь чинится основной
    сценарий issue #3 (0 -> 50 А на датчике 1:2000).
    """
    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_manual_range_cfg, rm=rm)
    dmm.current_range_idx = 0  # range 0.0002 — заведомо занижен

    dmm.auto_range(0.015, is_first=True)  # реальный выход, соответствующий 50А/1:2000 = 0.025, здесь просто похожая величина

    assert dmm.ranges[dmm.current_range_idx] == 0.02
    assert fake.written[-1] == 'SENS:CURR:DC:RANG 0.02'


# ----------------------------------------------------------------------
# is_overflow_reading
# ----------------------------------------------------------------------

def test_is_overflow_reading_detects_scpi_sentinel():
    assert is_overflow_reading(9.9e37) is True
    assert is_overflow_reading(-9.9e37) is True


def test_is_overflow_reading_false_for_real_currents():
    assert is_overflow_reading(0.0) is False
    assert is_overflow_reading(-2.5) is False
    assert is_overflow_reading(1020.0) is False  # даже паспортный максимум источника — не сентинел


def test_is_overflow_reading_false_for_nan():
    # NaN — сбой связи (см. measurement._measure_branch), не переполнение.
    # Не должен путаться с сентинелом при агрегированной обработке.
    assert is_overflow_reading(float('nan')) is False


# ----------------------------------------------------------------------
# parse_scpi_number (Ф1 п.5 — GPP-4323 VOUT?/IOUT? отвечают с суффиксом единиц)
# ----------------------------------------------------------------------

def test_parse_scpi_number_plain_float_string():
    assert parse_scpi_number("0.001234") == pytest.approx(0.001234)


def test_parse_scpi_number_strips_voltage_suffix():
    assert parse_scpi_number("00.000V") == pytest.approx(0.0)


def test_parse_scpi_number_strips_current_suffix():
    assert parse_scpi_number("0.0000A") == pytest.approx(0.0)


def test_parse_scpi_number_handles_negative_values_with_suffix():
    assert parse_scpi_number("-12.340V") == pytest.approx(-12.34)


def test_parse_scpi_number_handles_scientific_notation():
    assert parse_scpi_number("1.5E-03") == pytest.approx(0.0015)


def test_parse_scpi_number_raises_on_no_number_at_all():
    with pytest.raises(ValueError):
        parse_scpi_number("ERROR")


# ----------------------------------------------------------------------
# set_range — задержка на устаканивание в ручном режиме (Ф1 п.6)
# ----------------------------------------------------------------------

def test_set_range_sleeps_default_delay_in_manual_mode(akip2101_manual_range_cfg, make_fake_rm, monkeypatch):
    import instruments as instruments_module

    sleeps = []
    monkeypatch.setattr(instruments_module.time, 'sleep', lambda s: sleeps.append(s))

    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    # Фикстура зануляет range_settle_delay ради скорости остальных тестов —
    # здесь проверяем именно ненулевую задержку, поэтому убираем override.
    cfg = json.loads(akip2101_manual_range_cfg.read_text(encoding='utf-8'))
    del cfg['range_settle_delay']
    override_path = akip2101_manual_range_cfg.parent / "akip2101_manual_default_delay.json"
    override_path.write_text(json.dumps(cfg), encoding='utf-8')

    dmm = Multimeter("FAKE::ADDR", override_path, rm=rm)
    sleeps.clear()  # интересует только вызов из set_range(), не из _init_device()

    dmm.set_range(0.02)

    assert 0.7 in sleeps


def test_set_range_delay_is_configurable(akip2101_manual_range_cfg, make_fake_rm, monkeypatch):
    import instruments as instruments_module

    sleeps = []
    monkeypatch.setattr(instruments_module.time, 'sleep', lambda s: sleeps.append(s))

    cfg = json.loads(akip2101_manual_range_cfg.read_text(encoding='utf-8'))
    cfg['range_settle_delay'] = 1.5
    override_path = akip2101_manual_range_cfg.parent / "akip2101_manual_custom_delay.json"
    override_path.write_text(json.dumps(cfg), encoding='utf-8')

    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", override_path, rm=rm)
    sleeps.clear()

    dmm.set_range(0.02)

    assert 1.5 in sleeps


def test_set_range_does_not_sleep_in_auto_mode(akip2101_cfg, make_fake_rm, monkeypatch):
    import instruments as instruments_module

    sleeps = []
    monkeypatch.setattr(instruments_module.time, 'sleep', lambda s: sleeps.append(s))

    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", akip2101_cfg, rm=rm)
    sleeps.clear()

    dmm.set_range(0.02)  # no-op в авто-режиме — не должен ни писать в порт, ни спать

    assert sleeps == []


# ----------------------------------------------------------------------
# set_range — стиль {index} (RIGOL DM3068 в режиме вольтметра, Ф1 п.6)
# ----------------------------------------------------------------------

def test_set_range_supports_index_style_command(instruments_dir, tmp_path, make_fake_rm):
    """
    range_command может параметризоваться либо значением диапазона
    ({range_val} — стиль АКИП), либо его порядковым индексом ({index} —
    стиль RIGOL DM3068 в режиме вольтметра, см. IDM-DNKMetr). Оба
    плейсхолдера должны поддерживаться без завязки на то, какой из них
    реально есть в строке команды.
    """
    cfg = {
        "model_name": "synthetic index-style DMM",
        "keywords": ["SYNTH"],
        "init_commands": [],
        "measure_command": "MEAS?",
        "manual_range": True,
        "range_command": ":MEASure:VOLTage:DC {index}",
        "range_settle_delay": 0,
        "ranges": [0.2, 2.0, 20.0, 200.0, 1000.0],
    }
    path = tmp_path / "index_style.json"
    path.write_text(json.dumps(cfg), encoding='utf-8')

    fake = FakeVisaResource()
    rm = make_fake_rm({"FAKE::ADDR": fake})
    dmm = Multimeter("FAKE::ADDR", path, rm=rm)

    dmm.current_range_idx = 2
    dmm.set_range(dmm.ranges[2])  # значение здесь не используется командой вовсе

    assert fake.written[-1] == ':MEASure:VOLTage:DC 2'


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


def test_voltage_source_output_on_off_prefers_verified_all_output_commands(instruments_dir, make_fake_rm):
    """
    Регрессия Ф1 п.5: ALLOUTON/ALLOUTOFF проверены вживую в IDM-DNKMetr,
    :OUTPut{ch}:STATe ON/OFF — никогда. gpp74323.json объявляет
    all_output_on/all_output_off, поэтому именно они должны уйти в порт.
    """
    cfg_path = instruments_dir / "voltage_sources" / "gpp74323.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = VoltageSource("A", cfg_path, rm=rm)
    src.output_on()
    src.output_off()
    assert "ALLOUTON" in fake.written
    assert "ALLOUTOFF" in fake.written
    assert ":OUTPut1:STATe ON" not in fake.written
    assert ":OUTPut1:STATe OFF" not in fake.written


def test_voltage_source_output_on_off_falls_back_to_per_channel_command(instruments_dir, tmp_path, make_fake_rm):
    """Конфиг без all_output_on/off (гипотетическая другая модель) должен продолжать работать через {ch}."""
    cfg = json.loads((instruments_dir / "voltage_sources" / "gpp74323.json").read_text(encoding='utf-8'))
    del cfg['all_output_on']
    del cfg['all_output_off']
    path = tmp_path / "gpp_no_allout.json"
    path.write_text(json.dumps(cfg), encoding='utf-8')

    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})
    src = VoltageSource("A", path, rm=rm)
    src.output_on()
    src.output_off()

    assert ":OUTPut1:STATe ON" in fake.written
    assert ":OUTPut1:STATe OFF" in fake.written


# ----------------------------------------------------------------------
# VoltageSource — readback с суффиксом единиц (Ф1 п.5)
# ----------------------------------------------------------------------

def test_voltage_source_measure_voltage_strips_unit_suffix(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "voltage_sources" / "gpp74323.json"
    fake = FakeVisaResource(query_responses=["00.000V"])
    rm = make_fake_rm({"A": fake})

    src = VoltageSource("A", cfg_path, rm=rm)
    value = src.measure_voltage()

    assert value == pytest.approx(0.0)
    assert fake.queried[-1] == "VOUT1?"


def test_voltage_source_measure_current_strips_unit_suffix(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "voltage_sources" / "gpp74323.json"
    fake = FakeVisaResource(query_responses=["0.0000A"])
    rm = make_fake_rm({"A": fake})

    src = VoltageSource("A", cfg_path, rm=rm)
    value = src.measure_current()

    assert value == pytest.approx(0.0)
    assert fake.queried[-1] == "IOUT1?"


def test_voltage_source_measure_voltage_handles_nonzero_reading_with_suffix(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "voltage_sources" / "gpp74323.json"
    fake = FakeVisaResource(query_responses=["12.3400V"])
    rm = make_fake_rm({"A": fake})

    src = VoltageSource("A", cfg_path, rm=rm)
    assert src.measure_voltage() == pytest.approx(12.34)


# ----------------------------------------------------------------------
# VoltageSource — baud_rate для ASRL (Ф1 п.5)
# ----------------------------------------------------------------------

def test_voltage_source_sets_baud_rate_from_config(instruments_dir, make_fake_rm):
    cfg_path = instruments_dir / "voltage_sources" / "gpp74323.json"
    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})

    src = VoltageSource("A", cfg_path, rm=rm)

    assert fake.baud_rate == 115200


def test_voltage_source_without_baud_rate_field_does_not_set_it(instruments_dir, tmp_path, make_fake_rm):
    cfg = json.loads((instruments_dir / "voltage_sources" / "gpp74323.json").read_text(encoding='utf-8'))
    del cfg['baud_rate']
    path = tmp_path / "gpp_no_baud.json"
    path.write_text(json.dumps(cfg), encoding='utf-8')

    fake = FakeVisaResource()
    rm = make_fake_rm({"A": fake})
    VoltageSource("A", path, rm=rm)

    assert not hasattr(fake, 'baud_rate')


# ----------------------------------------------------------------------
# discover_instruments
# ----------------------------------------------------------------------

def test_discover_instruments_finds_dmm_and_source(instruments_dir):
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    src_res = FakeVisaResource(idn="ITECH,IT-M3122,SN002,1.0")
    rm = FakeResourceManager({"DMM_ADDR": dmm_res, "SRC_ADDR": src_res})

    dmm_addr, dmm_cfg, src_addr, src_cfg = discover_instruments(
        instruments_dir / "multimeters_current", instruments_dir / "current_sources", rm=rm,
    )

    assert dmm_addr == "DMM_ADDR"
    assert dmm_cfg.name == "akip2101.json"
    assert src_addr == "SRC_ADDR"
    assert src_cfg.name == "akip1162.json"


def test_discover_instruments_skips_relay_port(instruments_dir):
    """
    п.4: ASRL-ресурс платы реле НЕ опрашивается *IDN? (плата не отвечает на
    SCPI, открытие её порта — лишний таймаут/сброс). Реле-ресурс, который бы
    бросил при query, должен быть пропущен — а обычные приборы найдены.
    """
    relay_res = FakeVisaResource()  # без idn -> query('*IDN?') бросит AssertionError
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    src_res = FakeVisaResource(idn="ITECH,IT-M3122,SN002,1.0")
    rm = FakeResourceManager({"ASRL3::INSTR": relay_res, "DMM_ADDR": dmm_res, "SRC_ADDR": src_res})

    dmm_addr, _, src_addr, _ = discover_instruments(
        instruments_dir / "multimeters_current", instruments_dir / "current_sources", rm=rm,
        exclude_relay_port="COM3",
    )
    assert dmm_addr == "DMM_ADDR"
    assert src_addr == "SRC_ADDR"
    # плату реле не трогали *IDN?-опросом
    assert relay_res.queried == []


def test_discover_instruments_raises_when_no_resources(instruments_dir):
    from tests.conftest import FakeResourceManager
    rm = FakeResourceManager({})
    with pytest.raises(RuntimeError):
        discover_instruments(instruments_dir / "multimeters_current", instruments_dir / "current_sources", rm=rm)


def test_discover_instruments_raises_when_source_missing(instruments_dir):
    dmm_res = FakeVisaResource(idn="SIGLENT,SDM3055,SN001,1.0")
    rm = FakeResourceManager({"DMM_ADDR": dmm_res})

    with pytest.raises(RuntimeError, match="источник"):
        discover_instruments(instruments_dir / "multimeters_current", instruments_dir / "current_sources", rm=rm)


# ----------------------------------------------------------------------
# identify_instrument (п.11 — "мигнуть")
# ----------------------------------------------------------------------

def test_identify_returns_false_when_config_has_no_identify_command():
    # Ни один реальный конфиг в репозитории пока не объявляет
    # identify_command (не сочиняем непроверенные SCPI-команды) — это
    # штатный, ожидаемый случай, не ошибка.
    rm = FakeResourceManager({"ADDR": FakeVisaResource()})
    assert identify_instrument(rm, "ADDR", {}) is False


def test_identify_sends_configured_command_and_returns_true():
    res = FakeVisaResource()
    rm = FakeResourceManager({"ADDR": res})
    ok = identify_instrument(rm, "ADDR", {"identify_command": "DISP:TEXT 'HELLO'"})
    assert ok is True
    assert res.written == ["DISP:TEXT 'HELLO'"]


def test_identify_closes_resource_even_if_write_fails():
    class DyingResource(FakeVisaResource):
        def write(self, cmd):
            raise RuntimeError("порт занят")

    res = DyingResource()
    rm = FakeResourceManager({"ADDR": res})
    with pytest.raises(RuntimeError):
        identify_instrument(rm, "ADDR", {"identify_command": "BLINK"})
    assert res.closed is True
