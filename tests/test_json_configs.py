import json

import pytest


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def _multimeter_configs(instruments_dir):
    return sorted((instruments_dir / "multimeters").glob("*.json"))


def _current_source_configs(instruments_dir):
    return sorted((instruments_dir / "current_sources").glob("*.json"))


def _voltage_source_configs(instruments_dir):
    return sorted((instruments_dir / "voltage_sources").glob("*.json"))


def test_all_json_configs_are_valid_json(instruments_dir):
    files = list(instruments_dir.glob("**/*.json"))
    assert files, "Не найдено ни одного конфига приборов"
    for f in files:
        _load(f)  # не должно бросить исключение


@pytest.mark.parametrize("get_files", [_multimeter_configs])
def test_multimeter_configs_have_required_keys(instruments_dir, get_files):
    for f in get_files(instruments_dir):
        cfg = _load(f)
        for key in ("model_name", "keywords", "init_commands", "measure_command", "ranges"):
            assert key in cfg, f"{f.name}: отсутствует ключ '{key}'"
        assert isinstance(cfg["keywords"], list) and cfg["keywords"], f"{f.name}: keywords пуст"
        assert isinstance(cfg["ranges"], list) and cfg["ranges"], f"{f.name}: ranges пуст"


def test_multimeter_ranges_are_strictly_ascending(instruments_dir):
    for f in _multimeter_configs(instruments_dir):
        cfg = _load(f)
        ranges = cfg["ranges"]
        assert ranges == sorted(ranges), f"{f.name}: ranges должны быть отсортированы по возрастанию"
        assert len(set(ranges)) == len(ranges), f"{f.name}: ranges содержат дубликаты"
        assert all(r > 0 for r in ranges), f"{f.name}: диапазоны должны быть положительными"


def test_multimeter_measure_command_does_not_reset_range(instruments_dir):
    """
    Регрессия ключевого бага: MEAS?/CONF? по SCPI переконфигурируют прибор и
    сбрасывают диапазон в AUTO при каждом чтении, из-за чего ручной
    set_range()/auto_range() в instruments.py/measurement.py перестают
    работать. Актуально только для manual_range: true — в авто-режиме
    (по умолчанию) MEAS?/CONF? не проблема, диапазон и так авто.
    """
    for f in _multimeter_configs(instruments_dir):
        cfg = _load(f)
        if not cfg.get("manual_range", False):
            continue
        cmd = cfg["measure_command"].strip().upper()
        assert not cmd.startswith("MEAS"), f"{f.name}: measure_command не должен быть MEAS? (сбрасывает диапазон)"
        assert not cmd.startswith("CONF"), f"{f.name}: measure_command не должен быть CONF? (сбрасывает диапазон)"
        assert cmd in ("READ?", "FETC?", "FETCH?"), f"{f.name}: неожиданный measure_command {cmd!r}"


def test_multimeter_manual_range_requires_disable_autorange_and_range_command(instruments_dir):
    """
    Ручной диапазон (Multimeter.set_range/auto_range) — опциональная
    возможность, по умолчанию выключена (авто-диапазон прибора). Если она
    явно включена (manual_range: true), конфиг обязан нести и команду
    отключения автодиапазона, и шаблон команды установки диапазона —
    иначе instruments.py упадёт с KeyError либо будет конкурировать с
    встроенной автоматикой прибора.
    """
    for f in _multimeter_configs(instruments_dir):
        cfg = _load(f)
        if not cfg.get("manual_range", False):
            continue
        disable_cmd = cfg.get("disable_autorange_command", "").upper()
        assert "RANG:AUTO" in disable_cmd and "OFF" in disable_cmd, \
            f"{f.name}: manual_range: true требует disable_autorange_command (RANG:AUTO OFF)"
        assert "range_command" in cfg and "{range_val}" in cfg["range_command"], \
            f"{f.name}: manual_range: true требует range_command с плейсхолдером {{range_val}}"


def test_current_source_configs_have_required_keys(instruments_dir):
    for f in _current_source_configs(instruments_dir):
        cfg = _load(f)
        for key in ("model_name", "keywords", "init_commands", "setup_commands", "output_on", "output_off"):
            assert key in cfg, f"{f.name}: отсутствует ключ '{key}'"
        for key in ("voltage_limit", "current"):
            assert key in cfg["setup_commands"], f"{f.name}: setup_commands.{key} отсутствует"


def test_current_source_configs_declare_hardware_limits(instruments_dir):
    # max_current/max_voltage — паспортные пределы источника. Без них нечего
    # проверять при вводе X_stop/V_limit (см. limits.py и cli.validate_measure_params).
    for f in _current_source_configs(instruments_dir):
        cfg = _load(f)
        assert "max_current" in cfg, f"{f.name}: отсутствует max_current"
        assert "max_voltage" in cfg, f"{f.name}: отсутствует max_voltage"
        assert cfg["max_current"] > 0, f"{f.name}: max_current должен быть положительным"
        assert cfg["max_voltage"] > 0, f"{f.name}: max_voltage должен быть положительным"


def test_voltage_source_configs_have_required_keys(instruments_dir):
    for f in _voltage_source_configs(instruments_dir):
        cfg = _load(f)
        for key in ("model_name", "keywords", "init_commands", "channels",
                    "tracking_series_command", "setup_commands", "output_on", "output_off"):
            assert key in cfg, f"{f.name}: отсутствует ключ '{key}'"
        assert "primary" in cfg["channels"], f"{f.name}: channels.primary отсутствует"
        for key in ("voltage", "current_limit"):
            assert key in cfg["setup_commands"], f"{f.name}: setup_commands.{key} отсутствует"
        assert "{ch}" in cfg["output_on"], f"{f.name}: output_on должен параметризоваться по каналу"
        assert "{ch}" in cfg["output_off"], f"{f.name}: output_off должен параметризоваться по каналу"


def test_all_configs_have_distinct_nonempty_keywords_within_their_directory(instruments_dir):
    """keywords не должны пересекаться внутри одной папки — иначе find_config_for_idn неоднозначен."""
    for get_files in (_multimeter_configs, _current_source_configs, _voltage_source_configs):
        files = get_files(instruments_dir)
        seen = {}
        for f in files:
            cfg = _load(f)
            for kw in cfg["keywords"]:
                kw_norm = kw.upper()
                assert kw_norm not in seen, (
                    f"keyword {kw!r} встречается и в {seen.get(kw_norm)!r}, и в {f.name!r} — "
                    f"find_config_for_idn будет неоднозначным"
                )
                seen[kw_norm] = f.name
