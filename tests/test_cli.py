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


def test_validate_allows_stop_below_start_descending_sweep():
    # п.17: 250->0 (по модулю убывающий проход) не менее корректен, чем
    # 0->250 — планировщик (sweep.py) сам разбирается с порядком/знаком,
    # validate_measure_params больше не блокирует это как ошибку ввода.
    p = _good_current_params(); p['X_start'] = 10; p['X_stop'] = 5
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert errors == []


def test_validate_still_rejects_missing_endpoints():
    p = _good_current_params(); p['X_start'] = None
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert any('заданы' in e for e in errors)


def test_validate_rejects_negative_delays():
    p = _good_current_params(); p['delay'] = -1; p['cooling_delay'] = -2
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert len(errors) >= 2


def test_validate_requires_positive_vlimit_only_for_current():
    p = _good_current_params(); p['V_limit'] = 0
    assert any('напряжения' in e for e in validate_measure_params(p, 'current', current_source_limits={}))
    # для напряжения V_limit не проверяется (X_stop=60 — в пределах рабочего потолка п.35)
    pv = {'X_start': 0.0, 'X_stop': 60.0, 'X_step': 4.0,
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
    # voltage_source_limits={} изолирует тест от отдельной, но тоже вполне
    # реальной проверки паспортного напряжения источника, а 900 В неизбежно
    # заденет и рабочий потолок 60 В (п.35) — тест проверяет ИМЕННО
    # отсутствие ошибки про реле, а не полное отсутствие ошибок вообще.
    pv = {'X_start': 0.0, 'X_stop': 900.0, 'X_step': 4.0,
          'delay': 1.0, 'cooling_delay': 0.5, 'V_limit': 0.0}
    errors = validate_measure_params(pv, 'voltage', current_source_limits={}, voltage_source_limits={})
    assert not any('реле' in e for e in errors)


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
# validate_measure_params — паспортный потолок источника напряжения (Ф1 п.5)
# ----------------------------------------------------------------------

def _voltage_params(**overrides):
    p = {'X_start': 0.0, 'X_stop': 30.0, 'X_step': 5.0,
         'delay': 0.5, 'cooling_delay': 0.5, 'V_limit': 0.0}
    p.update(overrides)
    return p


def test_validate_rejects_x_stop_above_voltage_source_max_voltage():
    p = _voltage_params(X_stop=70.0)
    errors = validate_measure_params(
        p, 'voltage', voltage_source_limits={'max_voltage': 64.0},
    )
    assert any('64' in e and 'напряжен' in e for e in errors)


def test_validate_allows_x_stop_at_exactly_voltage_source_max_voltage():
    # max_voltage источника ниже рабочего потолка 60 В (п.35) — иначе даже
    # "ровно на паспортном пределе" уже упёрлось бы в ceiling, что было бы
    # уже про другую причину отказа.
    p = _voltage_params(X_stop=50.0)
    errors = validate_measure_params(
        p, 'voltage', voltage_source_limits={'max_voltage': 50.0},
    )
    assert errors == []


def test_validate_missing_voltage_source_limit_field_is_not_checked():
    p = _voltage_params(X_stop=55.0)
    errors = validate_measure_params(
        p, 'voltage', voltage_source_limits={'max_voltage': None},
    )
    assert errors == []


# ----------------------------------------------------------------------
# validate_measure_params — рабочий потолок 60 В (п.35, независимо от
# паспортного предела конкретного источника)
# ----------------------------------------------------------------------

def test_validate_blocks_voltage_above_60v_working_ceiling():
    p = _voltage_params(X_stop=61.0)
    errors = validate_measure_params(p, 'voltage', voltage_source_limits={'max_voltage': 64.0})
    assert any('60' in e for e in errors)


def test_validate_allows_voltage_at_exactly_60v_ceiling():
    p = _voltage_params(X_stop=60.0)
    errors = validate_measure_params(p, 'voltage', voltage_source_limits={'max_voltage': 64.0})
    assert errors == []


def test_validate_60v_ceiling_applies_even_if_source_paspportny_limit_is_higher():
    # Паспорт источника (гипотетически) допускает 100 В — рабочий потолок
    # 60 В всё равно строже и блокирует независимо от паспорта источника.
    p = _voltage_params(X_stop=70.0)
    errors = validate_measure_params(p, 'voltage', voltage_source_limits={'max_voltage': 100.0})
    assert any('60' in e for e in errors)


def test_validate_voltage_source_limit_not_checked_for_current_excitation():
    # Предел источника напряжения не имеет отношения к возбуждению током.
    p = _good_current_params()
    errors = validate_measure_params(
        p, 'current', current_source_limits={}, voltage_source_limits={'max_voltage': 1.0},
    )
    assert errors == []


def test_validate_reads_real_voltage_source_configs_by_default():
    # Без явного voltage_source_limits функция сама читает
    # instruments/voltage_sources/*.json (сейчас — GPP-4323, 64 В).
    p = _voltage_params(X_stop=100.0)
    errors = validate_measure_params(p, 'voltage', current_source_limits={})
    assert any('источника' in e for e in errors)


def test_validate_voltage_limit_uses_max_abs_of_start_and_stop():
    # X_stop=0, X_start=-70: раньше проверялся только X_stop (=0, "прошёл
    # бы"), хотя реально уставка источника на этой развёртке доходит до 70 В.
    p = _voltage_params(X_start=-70.0, X_stop=0.0)
    errors = validate_measure_params(p, 'voltage', voltage_source_limits={'max_voltage': 64.0})
    assert any('70' in e for e in errors)


def test_validate_voltage_limit_ok_when_max_abs_within_bounds():
    p = _voltage_params(X_start=-50.0, X_stop=50.0)
    errors = validate_measure_params(p, 'voltage', voltage_source_limits={'max_voltage': 64.0})
    assert errors == []


# ----------------------------------------------------------------------
# validate_measure_params — витки (п.37)
# ----------------------------------------------------------------------

def test_validate_rejects_nonpositive_turns():
    p = _good_current_params(); p['turns'] = 0
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert any('витк' in e for e in errors)

    p['turns'] = -1
    errors = validate_measure_params(p, 'current', current_source_limits={})
    assert any('витк' in e for e in errors)


def test_validate_allows_missing_turns_defaults_elsewhere():
    p = _good_current_params()
    assert 'turns' not in p
    assert validate_measure_params(p, 'current', current_source_limits={}) == []


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
    # Значения по умолчанию для новых параметров (п.8/19/37/29), когда флаги
    # вообще не передавались.
    assert params['branch'] == 'both'
    assert params['preset'] == 'diverging'
    assert params['turns'] == 1.0
    assert params['averaging_count'] == 4
    assert params['averaging_delay'] == 0.0
    assert params['discard_first'] is True


def test_resolve_measure_params_branch_preset_turns_averaging_from_cli(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current",
        "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "TestSensor", "--yes",
        "--branch", "positive", "--preset", "converging", "--turns", "2000",
        "--avg-count", "8", "--avg-delay", "0.05", "--avg-keep-first",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    params = resolve_measure_params(args, mgr)

    assert params['branch'] == 'positive'
    assert params['preset'] == 'converging'
    assert params['turns'] == 2000.0
    assert params['averaging_count'] == 8
    assert params['averaging_delay'] == 0.05
    assert params['discard_first'] is False


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


def test_resolve_measure_params_stop_less_than_start_is_a_valid_descending_sweep(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current",
        "--start", "10", "--stop", "5", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "Descending", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    params = resolve_measure_params(args, mgr)
    assert params['X_start'] == 10
    assert params['X_stop'] == 5


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


def test_resolve_measure_params_output_type_defaults_to_current(tmp_path):
    # Ось А-1 (PLAN_V2.md): output_type независим от excitation_type и не
    # переспрашивается интерактивно — тихий дефолт сохраняет поведение всех
    # существующих сценариев, где флаг не передавался вовсе.
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current",
        "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "TestSensor", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")
    params = resolve_measure_params(args, mgr)
    assert params['output_type'] == 'current'


def test_resolve_measure_params_output_type_from_cli_flag(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current", "--output", "voltage",
        "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1",
        "--label", "TestSensor", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")
    params = resolve_measure_params(args, mgr)
    assert params['output_type'] == 'voltage'


def test_resolve_measure_params_voltage_excitation_ignores_vlimit(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "voltage",
        "--start", "0", "--stop", "50", "--step", "4",
        "--delay", "1", "--cool", "0.5",
        "--label", "VSensor", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")

    params = resolve_measure_params(args, mgr)
    assert params['excitation_type'] == 'voltage'
    assert params['X_stop'] == 50


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


# ----------------------------------------------------------------------
# analyze — новые флаги (Ф3: п.10/21/30)
# ----------------------------------------------------------------------

def test_analyze_parser_accepts_labels_xlsx_estimate_ratio_flags():
    parser = build_parser()
    args = parser.parse_args(["analyze", "--labels", "--xlsx", "--estimate-ratio"])
    assert args.labels is True
    assert args.xlsx is True
    assert args.estimate_ratio is True


def test_analyze_parser_flags_default_to_false():
    parser = build_parser()
    args = parser.parse_args(["analyze"])
    assert args.labels is False
    assert args.xlsx is False
    assert args.estimate_ratio is False


# ----------------------------------------------------------------------
# --suppress-warnings (п.38) — верхнеуровневый флаг, попадает в params
# ----------------------------------------------------------------------

def test_suppress_warnings_flag_defaults_to_false(tmp_path):
    parser = build_parser()
    args = _measure_args(parser, [
        "--excitation", "current", "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1", "--label", "T", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")
    params = resolve_measure_params(args, mgr)
    assert params['suppress_notifications'] is False


def test_suppress_warnings_flag_carries_into_params(tmp_path):
    parser = build_parser()
    args = parser.parse_args([
        "--suppress-warnings", "measure",
        "--excitation", "current", "--start", "0", "--stop", "10", "--step", "1",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1", "--label", "T", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")
    params = resolve_measure_params(args, mgr)
    assert params['suppress_notifications'] is True


def test_suppress_warnings_hides_relay_current_warning_print(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args([
        "--suppress-warnings", "measure",
        "--excitation", "current", "--start", "0", "--stop", "500", "--step", "50",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1", "--label", "T", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")
    resolve_measure_params(args, mgr)
    captured = capsys.readouterr()
    assert "⚠" not in captured.out


def test_without_suppress_warnings_relay_current_warning_is_printed(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args([
        "measure",
        "--excitation", "current", "--start", "0", "--stop", "500", "--step", "50",
        "--vlimit", "5", "--delay", "0.1", "--cool", "0.1", "--label", "T", "--yes",
    ])
    mgr = ConfigManager(tmp_path / "cfg.json")
    resolve_measure_params(args, mgr)
    captured = capsys.readouterr()
    assert "⚠" in captured.out


# ----------------------------------------------------------------------
# relay/discover — новые подкоманды (Ф4, п.13/25)
# ----------------------------------------------------------------------

def test_relay_parser_requires_direction_choice():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["relay", "sideways"])


def test_relay_parser_accepts_valid_directions():
    parser = build_parser()
    for direction in ("forward", "reverse", "off"):
        args = parser.parse_args(["relay", direction, "--yes"])
        assert args.direction == direction
        assert args.yes is True


def test_relay_parser_relay_port_optional_and_defaults_to_none():
    parser = build_parser()
    args = parser.parse_args(["relay", "off"])
    assert args.relay_port is None
    assert args.yes is False


def test_discover_subcommand_parses_with_no_extra_args():
    parser = build_parser()
    args = parser.parse_args(["discover"])
    assert args.command == "discover"


# ----------------------------------------------------------------------
# setpoint/identify/profile/calibration/config — новые подкоманды (Ф5,
# CLI-паритет п.34: п.40-CLI, п.11-CLI, п.39-CLI, п.3-UI-CLI, п.23-CLI)
# ----------------------------------------------------------------------

def test_setpoint_parser_accepts_signed_value_and_defaults():
    parser = build_parser()
    args = parser.parse_args(["setpoint", "-5.5"])
    assert args.value == -5.5
    assert args.excitation == "current"
    assert args.vlimit is None
    assert args.yes is False


def test_setpoint_parser_accepts_all_options():
    parser = build_parser()
    args = parser.parse_args([
        "setpoint", "10", "--excitation", "voltage", "--vlimit", "5",
        "--dmm-addr", "DMM1", "--src-addr", "SRC1", "--relay-port", "COM3", "--yes",
    ])
    assert args.value == 10.0
    assert args.excitation == "voltage"
    assert args.vlimit == 5.0
    assert args.dmm_addr == "DMM1"
    assert args.src_addr == "SRC1"
    assert args.relay_port == "COM3"
    assert args.yes is True


def test_identify_parser_requires_address():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["identify"])
    args = parser.parse_args(["identify", "USB0::0x1234::INSTR"])
    assert args.address == "USB0::0x1234::INSTR"


def test_profile_list_parser():
    parser = build_parser()
    args = parser.parse_args(["profile", "list"])
    assert args.profile_command == "list"
    assert args.excitation is None

    args = parser.parse_args(["profile", "list", "--excitation", "voltage"])
    assert args.excitation == "voltage"


def test_profile_delete_parser():
    parser = build_parser()
    args = parser.parse_args(["profile", "delete", "MySensor", "--yes"])
    assert args.profile_command == "delete"
    assert args.name == "MySensor"
    assert args.yes is True


def test_profile_rename_parser():
    parser = build_parser()
    args = parser.parse_args(["profile", "rename", "Old", "New"])
    assert args.profile_command == "rename"
    assert args.old_name == "Old"
    assert args.new_name == "New"


def test_profile_requires_a_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["profile"])


def test_calibration_list_parser():
    parser = build_parser()
    args = parser.parse_args(["calibration", "list"])
    assert args.calibration_command == "list"


def test_calibration_set_parser():
    parser = build_parser()
    args = parser.parse_args([
        "calibration", "set", "akip2101.json", "--date", "2026-01-01", "--interval-months", "12",
    ])
    assert args.calibration_command == "set"
    assert args.config_file == "akip2101.json"
    assert args.date == "2026-01-01"
    assert args.interval_months == 12


def test_calibration_set_requires_date_and_interval():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["calibration", "set", "akip2101.json"])


def test_config_show_parser():
    parser = build_parser()
    args = parser.parse_args(["config", "show"])
    assert args.config_command == "show"


def test_config_set_work_dir_parser(tmp_path):
    parser = build_parser()
    args = parser.parse_args(["config", "set-work-dir", str(tmp_path)])
    assert args.config_command == "set-work-dir"
    assert args.path == tmp_path


def test_config_reset_work_dir_parser():
    parser = build_parser()
    args = parser.parse_args(["config", "reset-work-dir"])
    assert args.config_command == "reset-work-dir"


def test_config_requires_a_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["config"])


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
