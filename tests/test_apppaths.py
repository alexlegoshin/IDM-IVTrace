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
# install_root/read_install_record/write_install_record (Ф6, будущий
# инсталлятор/апдейтер) — изолируем от настоящей записи на диске
# (%LOCALAPPDATA%\Legoshi\IVTrace\install_info.json), чтобы тесты не
# читали/не писали реальную запись на машине, где они запускаются.
# ----------------------------------------------------------------------

def _isolate_install_record(monkeypatch, tmp_path):
    monkeypatch.setattr(apppaths, "_install_record_path", lambda: tmp_path / "install_info.json")


def test_install_root_defaults_to_app_root_without_record(monkeypatch, tmp_path):
    _isolate_install_record(monkeypatch, tmp_path)
    assert apppaths.install_root() == apppaths.app_root()


def test_read_install_record_missing_file_returns_none(monkeypatch, tmp_path):
    _isolate_install_record(monkeypatch, tmp_path)
    assert apppaths.read_install_record() is None


def test_read_install_record_corrupt_file_returns_none(monkeypatch, tmp_path):
    _isolate_install_record(monkeypatch, tmp_path)
    path = tmp_path / "install_info.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding='utf-8')
    assert apppaths.read_install_record() is None


def test_write_then_read_install_record_roundtrip(monkeypatch, tmp_path):
    _isolate_install_record(monkeypatch, tmp_path)
    chosen = tmp_path / "chosen" / "Legoshi" / "IVTrace"
    chosen.mkdir(parents=True)
    apppaths.write_install_record(chosen, tag="v2.0", release_date="2026-08-10T12:00:00Z")
    record = apppaths.read_install_record()
    assert record['install_root'] == str(chosen)
    assert record['tag'] == "v2.0"
    assert record['release_date'] == "2026-08-10T12:00:00Z"


def test_write_install_record_defaults_release_date_to_none(monkeypatch, tmp_path):
    # Офлайн-установка из уже скачанного архива — без обращения к GitHub
    # API дата релиза неизвестна, апдейтер сравнивает только по тегу.
    _isolate_install_record(monkeypatch, tmp_path)
    chosen = tmp_path / "chosen"
    chosen.mkdir(parents=True)
    apppaths.write_install_record(chosen, tag="v2.0")
    assert apppaths.read_install_record()['release_date'] is None


def test_install_root_uses_recorded_path_when_it_exists(monkeypatch, tmp_path):
    _isolate_install_record(monkeypatch, tmp_path)
    chosen = tmp_path / "chosen" / "Legoshi" / "IVTrace"
    chosen.mkdir(parents=True)
    apppaths.write_install_record(chosen, tag="v2.0")
    assert apppaths.install_root() == chosen


def test_install_root_falls_back_to_app_root_when_recorded_path_vanished(monkeypatch, tmp_path):
    # Инсталлятор записал путь, но папку потом снесли/перенесли руками —
    # не должно ронять приложение, просто ведём себя как без записи вовсе.
    _isolate_install_record(monkeypatch, tmp_path)
    vanished = tmp_path / "chosen" / "Legoshi" / "IVTrace"
    apppaths.write_install_record(vanished, tag="v2.0")  # не создаём саму папку
    assert apppaths.install_root() == apppaths.app_root()


def test_config_dir_and_default_data_dir_follow_install_root(monkeypatch, tmp_path):
    _isolate_install_record(monkeypatch, tmp_path)
    chosen = tmp_path / "chosen" / "Legoshi" / "IVTrace"
    chosen.mkdir(parents=True)
    apppaths.write_install_record(chosen, tag="v2.0")
    assert apppaths.config_dir() == chosen / "config"
    assert apppaths.default_data_dir() == chosen / "data"


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


# ----------------------------------------------------------------------
# clear_results_cache (баг-репорт: "очистка кэша" — CSV/PNG/XLSX + файл
# параметров последнего запуска в work_dir(), плюс содержимое Cache/;
# конфиги приборов и профили датчиков живут в других директориях и не
# должны быть даже видны этой функции).
# ----------------------------------------------------------------------

def _setup_work_dir(monkeypatch, tmp_path):
    _isolate_config_dir(monkeypatch, tmp_path)
    custom = tmp_path / "results"
    apppaths.set_work_dir(custom)
    custom.mkdir(parents=True, exist_ok=True)
    return custom


def test_clear_results_cache_removes_result_files_and_last_run_config(monkeypatch, tmp_path):
    base = _setup_work_dir(monkeypatch, tmp_path)
    (base / "IVtrace_Sensor1_20260101_120000.csv").write_text("data")
    (base / "IVtrace_Sensor1_20260101_120000.png").write_bytes(b"png")
    (base / "IVtrace_Sensor1_20260101_120000.xlsx").write_bytes(b"xlsx")
    (base / "IVtrace_Sensor1_20260101_120000_inverted.csv").write_text("data")
    (base / "ivtrace_config.json").write_text("{}")

    removed = apppaths.clear_results_cache()

    assert len(removed) == 5
    assert list(base.glob("IVtrace_*")) == []
    assert not (base / "ivtrace_config.json").exists()


def test_clear_results_cache_clears_cache_subfolder(monkeypatch, tmp_path):
    base = _setup_work_dir(monkeypatch, tmp_path)
    cache = base / "Cache"
    (cache / "nested").mkdir(parents=True)
    (cache / "nested" / "temp.png").write_bytes(b"x")

    removed = apppaths.clear_results_cache()

    assert base / "Cache" / "nested" / "temp.png" in removed
    assert not (cache / "nested" / "temp.png").exists()
    assert not (cache / "nested").exists()
    assert cache.is_dir()  # Cache сама остаётся, чистится только содержимое


def test_clear_results_cache_does_not_touch_instrument_configs_or_sensor_profiles(monkeypatch, tmp_path):
    base = _setup_work_dir(monkeypatch, tmp_path)
    (base / "IVtrace_x.csv").write_text("data")

    sensor_dir = apppaths.sensor_config_dir()
    sensor_dir.mkdir(parents=True, exist_ok=True)
    (sensor_dir / "MySensor.json").write_text("{}")

    apppaths.clear_results_cache()

    assert (sensor_dir / "MySensor.json").exists()
    assert apppaths.multimeter_cfg_dir().is_dir()
    assert any(apppaths.multimeter_cfg_dir().glob("*.json"))


def test_clear_results_cache_on_empty_dirs_removes_nothing(monkeypatch, tmp_path):
    _setup_work_dir(monkeypatch, tmp_path)
    assert apppaths.clear_results_cache() == []
