from config import ConfigManager


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
