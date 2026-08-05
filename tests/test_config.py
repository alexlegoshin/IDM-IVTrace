import pytest

from config import ConfigManager, SensorConfigManager


def test_save_and_load_round_trip(tmp_path):
    cfg_path = tmp_path / "sub" / "ivtrace_config.json"
    mgr = ConfigManager(cfg_path)

    data = {
        'excitation_type': 'current',
        'X_start': 0.0, 'X_stop': 100.0, 'X_step': 5.0,
        'V_limit': 3.0, 'delay': 1.0, 'cooling_delay': 1.5,
        'label': 'VAC 4646X100',
    }
    mgr.save(data)

    assert cfg_path.exists()
    loaded = mgr.load()
    assert loaded == data


def test_load_missing_file_returns_empty_dict(tmp_path):
    mgr = ConfigManager(tmp_path / "does_not_exist.json")
    assert mgr.load() == {}


def test_load_corrupt_json_returns_empty_dict_and_warns(tmp_path, capsys):
    cfg_path = tmp_path / "broken.json"
    cfg_path.write_text("{not valid json", encoding='utf-8')

    mgr = ConfigManager(cfg_path)
    result = mgr.load()

    assert result == {}
    captured = capsys.readouterr()
    assert "Предупреждение" in captured.out


def test_save_creates_parent_directory(tmp_path):
    cfg_path = tmp_path / "a" / "b" / "c" / "cfg.json"
    mgr = ConfigManager(cfg_path)
    mgr.save({'label': 'x'})
    assert cfg_path.exists()


# ----------------------------------------------------------------------
# SensorConfigManager — раскладка по подпапкам current/voltage (п.39)
# ----------------------------------------------------------------------

def _params(excitation_type='current'):
    return {
        'excitation_type': excitation_type,
        'X_start': 0.0, 'X_stop': 100.0, 'X_step': 5.0,
        'ratio': 2000.0, 'turns': 1.0,
    }


def test_creates_current_and_voltage_subdirs(tmp_path):
    SensorConfigManager(tmp_path / "sensors")
    assert (tmp_path / "sensors" / "current").is_dir()
    assert (tmp_path / "sensors" / "voltage").is_dir()


def test_save_current_sensor_goes_into_current_subdir(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    path = mgr.save_sensor_config("Датчик1", _params('current'))
    assert path.parent == tmp_path / "sensors" / "current"


def test_save_voltage_sensor_goes_into_voltage_subdir(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    path = mgr.save_sensor_config("Датчик1", _params('voltage'))
    assert path.parent == tmp_path / "sensors" / "voltage"


def test_excitation_type_arg_overrides_params_value(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    path = mgr.save_sensor_config("X", _params('current'), excitation_type='voltage')
    assert path.parent == tmp_path / "sensors" / "voltage"


def test_load_round_trip_within_same_excitation_type(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    mgr.save_sensor_config("Датчик1", _params('current'))
    loaded = mgr.load_sensor_config("Датчик1", excitation_type='current')
    assert loaded['ratio'] == 2000.0
    assert loaded['excitation_type'] == 'current'


def test_load_with_explicit_excitation_type_does_not_find_wrong_type(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    mgr.save_sensor_config("Датчик1", _params('current'))
    assert mgr.load_sensor_config("Датчик1", excitation_type='voltage') is None


def test_load_without_excitation_type_searches_both_subdirs(tmp_path):
    # Нужен для CLI --load-config: на этот момент excitation_type мог
    # придти ИЗ загружаемого профиля, ещё не определён заранее.
    mgr = SensorConfigManager(tmp_path / "sensors")
    mgr.save_sensor_config("Датчик1", _params('voltage'))
    loaded = mgr.load_sensor_config("Датчик1")
    assert loaded is not None
    assert loaded['excitation_type'] == 'voltage'


def test_load_missing_profile_returns_none(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    assert mgr.load_sensor_config("НетТакого") is None


def test_list_filters_by_excitation_type(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    mgr.save_sensor_config("ТокДатчик", _params('current'))
    mgr.save_sensor_config("НапряжениеДатчик", _params('voltage'))

    assert mgr.list_sensor_configs('current') == ['ТокДатчик']
    assert mgr.list_sensor_configs('voltage') == ['НапряжениеДатчик']


def test_list_without_filter_returns_configs_from_both_subdirs(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    mgr.save_sensor_config("ТокДатчик", _params('current'))
    mgr.save_sensor_config("НапряжениеДатчик", _params('voltage'))

    assert mgr.list_sensor_configs() == ['НапряжениеДатчик', 'ТокДатчик']


def test_delete_removes_only_from_matching_subdir(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    mgr.save_sensor_config("Общее", _params('current'))
    mgr.save_sensor_config("Общее", _params('voltage'))

    assert mgr.delete_sensor_config("Общее", excitation_type='current') is True
    assert mgr.load_sensor_config("Общее", excitation_type='current') is None
    assert mgr.load_sensor_config("Общее", excitation_type='voltage') is not None


def test_delete_without_excitation_type_removes_from_both(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    mgr.save_sensor_config("Общее", _params('current'))
    mgr.save_sensor_config("Общее", _params('voltage'))

    assert mgr.delete_sensor_config("Общее") is True
    assert mgr.load_sensor_config("Общее") is None


def test_delete_nonexistent_returns_false(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    assert mgr.delete_sensor_config("НетТакого") is False


# ----------------------------------------------------------------------
# SensorConfigManager — метаданные (п.39)
# ----------------------------------------------------------------------

def test_save_adds_meta_block_with_timestamp_and_version(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    mgr.save_sensor_config("Датчик1", _params('current'), comment="испытательный стенд 2")
    loaded = mgr.load_sensor_config("Датчик1", excitation_type='current')

    assert '_meta' in loaded
    assert loaded['_meta']['comment'] == "испытательный стенд 2"
    assert loaded['_meta']['saved_at']
    assert loaded['_meta']['app_version']


# ----------------------------------------------------------------------
# SensorConfigManager — безопасность имени файла (п.39)
# ----------------------------------------------------------------------

def test_rejects_path_traversal_in_name(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    with pytest.raises(ValueError):
        mgr.save_sensor_config("../../evil", _params())


def test_rejects_absolute_path_as_name(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    with pytest.raises(ValueError):
        mgr.save_sensor_config("/etc/passwd", _params())


def test_rejects_backslash_in_name(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    with pytest.raises(ValueError):
        mgr.save_sensor_config("a\\b", _params())


def test_rejects_empty_name(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    with pytest.raises(ValueError):
        mgr.save_sensor_config("   ", _params())


def test_allows_cyrillic_digits_space_dash_underscore(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    path = mgr.save_sensor_config("Датчик ДТ-100А1_v2", _params())
    assert path.exists()


def test_load_with_unsafe_name_returns_none_not_raises(tmp_path):
    # load — это часто автоматический путь (например --load-config из
    # старого сохранённого конфига), падать с исключением на кривом имени
    # хуже, чем вернуть "не найдено".
    mgr = SensorConfigManager(tmp_path / "sensors")
    assert mgr.load_sensor_config("../../etc/passwd") is None


def test_traversal_attempt_does_not_actually_escape_the_sensors_dir(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    outside_marker = tmp_path / "should_not_be_created.json"
    with pytest.raises(ValueError):
        mgr.save_sensor_config("../should_not_be_created", _params())
    assert not outside_marker.exists()


# ----------------------------------------------------------------------
# SensorConfigManager — неизвестный тип возбуждения
# ----------------------------------------------------------------------

def test_unknown_excitation_type_raises_value_error(tmp_path):
    mgr = SensorConfigManager(tmp_path / "sensors")
    with pytest.raises(ValueError):
        mgr.save_sensor_config("X", _params(), excitation_type='bogus')
