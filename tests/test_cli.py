import builtins

import pytest

from cli import build_parser, resolve_measure_params, make_csv_filename, validate_measure_params
from config import ConfigManager


# ----------------------------------------------------------------------
# validate_measure_params — единые правила проверки для CLI и GUI
# ----------------------------------------------------------------------

def _good_current_params():
    return {'X_start': 0.0, 'X_stop': 10.0, 'X_step': 1.0,
            'delay': 0.1, 'cooling_delay': 0.1, 'V_limit': 5.0}


# current_source_limits={} передаётся явно во всех тестах ниже, не
# связанных с проверкой пределов источника, — иначе validate_measure_params
# по умолчанию читает реальные instruments/current_sources/*.json с диска, и
# тест начинает молча зависеть от паспортных чисел в них (сейчас у
# АКИП-1162: 10 В/1020 А, но это деталь конфига, а не контракт функции).

def test_validate_ok_for_valid_current_params():
    assert validate_measure_params(_good_current_params(), 'current', current_source_limits={}) == []


def test_validate_rejects_zero_step():
    p = _good_current_params(); p['X_step'] = 0
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert any('Шаг' in e for e in errors)


def test_validate_rejects_stop_below_start():
    p = _good_current_params(); p['X_start'] = 10; p['X_stop'] = 5
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert any('Конечное' in e for e in errors)


def test_validate_rejects_negative_delays():
    p = _good_current_params(); p['delay'] = -1; p['cooling_delay'] = -2
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert len(errors) >= 2


def test_validate_requires_positive_vlimit_only_for_current():
    p = _good_current_params(); p['V_limit'] = 0
    assert any('напряжения' in e for e in validate_measure_params(p, 'current', current_source_limits={}))
    # для напряжения V_limit не проверяется
    pv = {'X_start': 0.0, 'X_stop': 64.0, 'X_step': 4.0,
          'delay': 1.0, 'cooling_delay': 0.5, 'V_limit': 0.0}
    assert validate_measure_params(pv, 'voltage', current_source_limits={}) == []


# ----------------------------------------------------------------------
# validate_measure_params — лимиты платы реле (п. 28) и паспорт источника
# ----------------------------------------------------------------------

def test_validate_blocks_current_above_relay_hard_limit():
    p = _good_current_params(); p['X_stop'] = 800.1
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert any('800' in e for e in errors)


def test_validate_allows_current_at_exactly_relay_hard_limit():
    p = _good_current_params(); p['X_stop'] = 800.0
    assert validate_measure_params(p, 'current', current_source_limits={}) == []


def test_validate_does_not_block_on_relay_limit_for_voltage_excitation():
    # Лимит реле — про ток. Возбуждение напряжением им не ограничивается.
    pv = {'X_start': 0.0, 'X_stop': 900.0, 'X_step': 4.0,
          'delay': 1.0, 'cooling_delay': 0.5, 'V_limit': 0.0}
    assert validate_measure_params(pv, 'voltage', current_source_limits={}) == []


def test_validate_ignores_relay_warning_threshold_it_is_not_an_error():
    # 400-800 А — предупреждение (relay_current_warning), не ошибка
    # validate_measure_params. Оно показывается отдельно, не блокирует.
    p = _good_current_params(); p['X_stop'] = 500.0
    assert validate_measure_params(p, 'current', current_source_limits={}) == []


def test_validate_rejects_vlimit_above_source_max_voltage():
    p = _good_current_params(); p['V_limit'] = 15.0
    errors = validate_measure_params(p, 'current', current_source_limits={'max_voltage': 10.0, 'max_current': None})
    assert any('10' in e and 'напряжен' in e for e in errors)


def test_validate_rejects_current_above_source_max_current():
    p = _good_current_params(); p['X_stop'] = 50.0
    errors = validate_measure_params(
        p, 'current', current_source_limits={'max_voltage': None, 'max_current': 20.0},
    )
    assert any('20' in e for e in errors)


def test_validate_missing_source_limit_field_is_not_checked():
    # {'max_voltage': None} означает "ни один известный источник поле не
    # заявил" — не должно трактоваться как предел 0.
    p = _good_current_params()
    errors = validate_measure_params(
        p, 'current', current_source_limits={'max_voltage': None, 'max_current': None},
    )
    assert errors == []


def test_validate_reads_real_current_source_configs_by_default():
    # Без явного current_source_limits функция сама читает
    # instruments/current_sources/*.json — так пользуется ею продакшен-код
    # (cli.resolve_measure_params, gui._gather_params).
    p = _good_current_params(); p['V_limit'] = 10_000.0
    errors = validate_measure_params(p, 'current')
    assert any('источника' in e for e in errors)


# ----------------------------------------------------------------------
# current_sweep_max_abs
# ----------------------------------------------------------------------

def test_current_sweep_max_abs_takes_the_larger_endpoint_by_magnitude():
    from cli import current_sweep_max_abs
    p = {'X_start': -250.0, 'X_stop': 100.0}
    assert current_sweep_max_abs(p, 'current') == 250.0


def test_current_sweep_max_abs_none_for_voltage_excitation():
    from cli import current_sweep_max_abs
    p = {'X_start': 0.0, 'X_stop': 64.0}
    assert current_sweep_max_abs(p, 'voltage') is None


def test_current_sweep_max_abs_none_when_params_incomplete():
    from cli import current_sweep_max_abs
    assert current_sweep_max_abs({'X_start': 0.0}, 'current') is None


def _measure_args(parser, extra_args):
    return parser.parse_args(["measure"] + extra_args)


# ----------------------------------------------------------------------
# make_csv_filename
# ----------------------------------------------------------------------

def test_make_csv_filename_sanitizes_label(tmp_path):
    path = make_csv_filename(tmp_path, "VAC 4646X100 #test!")
    assert path.parent == tmp_path
    assert path.name.startswith("IVtrace_VAC_4646X100_test_")
    assert path.suffix == ".csv"


def test_make_csv_filename_empty_label_uses_nolabel(tmp_path):
    path = make_csv_filename(tmp_path, "")
    assert "nolabel" in path.name


def test_make_csv_filename_none_label_uses_nolabel(tmp_path):
    path = make_csv_filename(tmp_path, None)
    assert "nolabel" in path.name


# ----------------------------------------------------------------------
# resolve_measure_params — путь без интерактивности (все флаги переданы)
# ----------------------------------------------------------------------

def test_resolve_measure_params_from_full_cli_args(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current",
        "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "TestSensor", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    params = resolve_measure_params(args, mgr)

    assert params['excitation_type'] == 'current'
    assert params['X_start'] == 0
    assert params['X_stop'] == 10
    assert params['X_step'] == 1
    assert params['V_limit'] == 5
    assert params['label'] == 'TestSensor'
    assert mgr.load() == params


def test_resolve_measure_params_step_zero_raises_value_error(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current",
        "--start", "0", "--stop", "10", "--step", "0",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "Bad", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    with pytest.raises(ValueError, match="Шаг"):
        resolve_measure_params(args, mgr)


def test_resolve_measure_params_stop_less_than_start_raises_value_error(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current",
        "--start", "10", "--stop", "5", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "Bad", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    with pytest.raises(ValueError, match="Конечное"):
        resolve_measure_params(args, mgr)


def test_resolve_measure_params_negative_vlimit_raises_value_error(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current",
        "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "-1", "--delay", "0.1", "--cool", "0.1",
        "--label", "Bad", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    with pytest.raises(ValueError, match="напряжения"):
        resolve_measure_params(args, mgr)


def test_resolve_measure_params_voltage_excitation_ignores_vlimit(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "voltage",
        "--start", "0", "--stop", "64", "--step", "4",
        "--delay", "1", "--cool", "0.5",
        "--label", "VSensor", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    params = resolve_measure_params(args, mgr)
    assert params['excitation_type'] == 'voltage'
    assert params['X_stop'] == 64


# ----------------------------------------------------------------------
# resolve_measure_params — интерактивный путь (валидатор должен перезапрашивать)
# ----------------------------------------------------------------------

def test_resolve_measure_params_interactive_reprompts_on_invalid_step(monkeypatch, tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current",
        "--start", "0", "--stop", "10",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "Interactive", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    # X_step не передан флагом -> будет запрошен интерактивно.
    # Сначала вводим невалидный 0 (должно перезапросить), затем валидный 2.
    inputs = iter(["0", "2"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(inputs))

    params = resolve_measure_params(args, mgr)
    assert params['X_step'] == 2


def test_resolve_measure_params_interactive_full_flow(monkeypatch, tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [])  # ничего не передано флагами
    mgr = ConfigManager(tmp_path / "cfg.json")

    # Порядок промптов: excitation_type, X_start, X_stop, X_step, V_limit,
    # delay, cooling_delay, label.
    inputs = iter(["current", "0", "10", "1", "5", "0.1", "0.1", "MySensor"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(inputs))

    params = resolve_measure_params(args, mgr)

    assert params['excitation_type'] == 'current'
    assert params['X_start'] == 0
    assert params['X_stop'] == 10
    assert params['X_step'] == 1
    assert params['V_limit'] == 5
    assert params['label'] == 'MySensor'
