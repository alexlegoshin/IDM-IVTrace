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
