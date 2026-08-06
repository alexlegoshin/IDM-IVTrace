import apppaths


def test_resource_base_exists():
    assert apppaths.resource_base().exists()


def test_instruments_dirs_point_to_real_folders():
    assert apppaths.instruments_dir().is_dir()
    assert apppaths.multimeter_cfg_dir().is_dir()
    assert apppaths.current_source_cfg_dir().is_dir()
    assert apppaths.voltage_source_cfg_dir().is_dir()


def test_tests_dir_is_this_folder():
    # Каталог tests/ должен существовать и содержать этот файл.
    tdir = apppaths.tests_dir()
    assert tdir.is_dir()
    assert (tdir / "test_apppaths.py").exists()


def test_default_data_dir_under_app_root():
    dd = apppaths.default_data_dir()
    assert dd.name == "data"
    assert dd.parent == apppaths.app_root()


# ----------------------------------------------------------------------
# work_dir/cache_dir/app settings (п.23) — изолируем от настоящего
# config_dir() (%LOCALAPPDATA%), чтобы тесты не читали/не писали реальные
# пользовательские настройки на машине, где они запускаются.
# ----------------------------------------------------------------------

def _isolate_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(apppaths, "config_dir", lambda: tmp_path / "config")


def test_work_dir_defaults_to_default_data_dir_without_override(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    assert apppaths.work_dir() == apppaths.default_data_dir()


def test_set_work_dir_overrides_work_dir(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    custom = tmp_path / "my_results"
    apppaths.set_work_dir(custom)
    assert apppaths.work_dir() == custom


def test_set_work_dir_none_clears_override(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    apppaths.set_work_dir(tmp_path / "custom")
    apppaths.set_work_dir(None)
    assert apppaths.work_dir() == apppaths.default_data_dir()


def test_work_dir_override_persists_across_settings_reads(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    custom = tmp_path / "persisted"
    apppaths.set_work_dir(custom)
    # Второе независимое чтение — не кэш в памяти, а то, что реально лежит на диске.
    assert apppaths.load_app_settings()['work_dir'] == str(custom)
    assert apppaths.work_dir() == custom


def test_set_work_dir_preserves_other_settings_keys(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    apppaths.save_app_settings({'unrelated_key': 'keep me'})
    apppaths.set_work_dir(tmp_path / "custom")
    settings = apppaths.load_app_settings()
    assert settings['unrelated_key'] == 'keep me'
    assert settings['work_dir'] == str(tmp_path / "custom")


def test_cache_dir_is_subfolder_of_work_dir(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    custom = tmp_path / "results"
    apppaths.set_work_dir(custom)
    assert apppaths.cache_dir() == custom / "Cache"


def test_load_app_settings_missing_file_returns_empty_dict(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    assert apppaths.load_app_settings() == {}


def test_load_app_settings_corrupt_file_returns_empty_dict(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    settings_path = tmp_path / "config" / "app_settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{not valid json", encoding='utf-8')
    assert apppaths.load_app_settings() == {}
