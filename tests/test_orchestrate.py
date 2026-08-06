import pandas as pd

from orchestrate import write_results_csv, _log_calibration_warnings, _resolve_instruments
from analysis import _read_metadata, load_and_analyze
from tests.conftest import FakeVisaResource, FakeResourceManager


def _params():
    return {
        'label': 'OrchSensor',
        'X_start': 0.0, 'X_stop': 10.0, 'X_step': 5.0,
        'V_limit': 5.0, 'delay': 0.1, 'cooling_delay': 0.2,
    }


def test_write_results_csv_header_and_data_roundtrip(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'X_set': 0.0, 'I_meas_A': 0.0},
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'X_set': 10.0, 'I_meas_A': 0.1},
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'reverse', 'X_set': -10.0, 'I_meas_A': -0.1},
    ])
    csv_path = tmp_path / "IVtrace_orch_20260101_000000.csv"

    write_results_csv(csv_path, df, _params(), excitation_type='current', unit='A')

    meta = _read_metadata(csv_path)
    assert meta['Датчик'] == 'OrchSensor'
    assert meta['Тип возбуждения'] == 'current'
    assert meta['Единица измерения возбуждения'] == 'A'
    assert meta['Всего точек'] == '3'
    assert 'Ограничение напряжения' in meta  # только для тока

    # Данные читаются штатным анализатором.
    back = pd.read_csv(csv_path, comment='#')
    assert list(back.columns) == ['Timestamp', 'Branch', 'X_set', 'I_meas_A']
    assert len(back) == 3


def test_write_results_csv_voltage_has_no_voltage_limit_line(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'X_set': 0.0, 'I_meas_A': 0.0},
    ])
    csv_path = tmp_path / "IVtrace_v_20260101_000000.csv"
    p = _params()
    write_results_csv(csv_path, df, p, excitation_type='voltage', unit='V')

    meta = _read_metadata(csv_path)
    assert meta['Тип возбуждения'] == 'voltage'
    assert 'Ограничение напряжения' not in meta


def test_written_csv_is_analyzable(tmp_path):
    df = pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'X_set': 0.0, 'I_meas_A': 0.0},
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'X_set': 10.0, 'I_meas_A': 0.1},
    ])
    csv_path = tmp_path / "IVtrace_ok_20260101_000000.csv"
    write_results_csv(csv_path, df, _params(), excitation_type='current', unit='A')

    stats = load_and_analyze(csv_path, I_nom=1000.0, X=100.0, save_png=False, show=False)
    assert stats['label'] == 'OrchSensor'
    assert stats['points'] == 2


# ----------------------------------------------------------------------
# output_type в шапке CSV и выбор роли мультиметра (ось А-1, PLAN_V2.md)
# ----------------------------------------------------------------------

def test_default_output_type_is_current_when_absent_from_params(tmp_path):
    # Старые вызовы/params без output_type вовсе — поведение как раньше.
    csv_path = tmp_path / "IVtrace_out_default_20260101_000000.csv"
    write_results_csv(csv_path, _one_row_df(), _params(), excitation_type='current', unit='A')
    meta = _read_metadata(csv_path)
    assert meta['Тип выхода датчика'] == 'current'
    assert meta['Единица измерения выхода'] == 'A'


def test_explicit_voltage_output_type_is_written_to_header(tmp_path):
    csv_path = tmp_path / "IVtrace_out_voltage_20260101_000000.csv"
    p = _params(); p['output_type'] = 'voltage'
    write_results_csv(csv_path, _one_row_df(), p, excitation_type='current', unit='A')
    meta = _read_metadata(csv_path)
    assert meta['Тип выхода датчика'] == 'voltage'
    assert meta['Единица измерения выхода'] == 'V'


def test_resolve_instruments_uses_ammeter_role_dir_by_default(instruments_dir):
    # instruments/multimeters_current/akip2101.json matches IDN 'AKIP-2101'
    # так же, как и instruments/multimeters_voltage/akip2101.json — оба
    # каталога знают про эту модель, поэтому единственный способ убедиться,
    # что выбран правильный КАТАЛОГ (а не просто "какой-то конфиг нашёлся"),
    # — сверить сам путь до найденного файла.
    dmm_dir = instruments_dir / "multimeters_current"
    src_dir = instruments_dir / "current_sources"
    rm = FakeResourceManager({
        "DMM": FakeVisaResource(idn="AKIP-2101"),
        "SRC": FakeVisaResource(idn="ITECH IT-M3130"),
    })
    dmm_addr, dmm_cfg, src_addr, src_cfg = _resolve_instruments(
        rm, 'current', "DMM", "SRC", src_dir, "источник тока", lambda msg: None, dmm_dir,
    )
    assert dmm_cfg.parent == dmm_dir


def test_resolve_instruments_uses_voltmeter_role_dir_when_requested(instruments_dir):
    dmm_dir = instruments_dir / "multimeters_voltage"
    src_dir = instruments_dir / "current_sources"
    rm = FakeResourceManager({
        "DMM": FakeVisaResource(idn="AKIP-2101"),
        "SRC": FakeVisaResource(idn="ITECH IT-M3130"),
    })
    dmm_addr, dmm_cfg, src_addr, src_cfg = _resolve_instruments(
        rm, 'current', "DMM", "SRC", src_dir, "источник тока", lambda msg: None, dmm_dir,
    )
    assert dmm_cfg.parent == dmm_dir


# ----------------------------------------------------------------------
# write_results_csv — branch/preset/turns в шапке (Ф2 п.8/19/37)
# ----------------------------------------------------------------------

def test_default_branch_describes_both_polarities_via_relay(tmp_path):
    csv_path = tmp_path / "IVtrace_branch_both_20260101_000000.csv"
    write_results_csv(csv_path, _one_row_df(), _params(), excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert 'Обе полярности сняты автоматически через плату реле' in text


def test_single_branch_names_which_polarity_was_measured(tmp_path):
    csv_path = tmp_path / "IVtrace_branch_pos_20260101_000000.csv"
    p = _params(); p['branch'] = 'positive'
    write_results_csv(csv_path, _one_row_df(), p, excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert 'Снята только одна полярность (positive)' in text
    assert 'Обе полярности' not in text


def test_non_default_preset_is_noted_only_for_both_branch(tmp_path):
    csv_path = tmp_path / "IVtrace_preset_20260101_000000.csv"
    p = _params(); p['branch'] = 'both'; p['preset'] = 'full_cycle'
    write_results_csv(csv_path, _one_row_df(), p, excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert 'Схема прохода: full_cycle' in text


def test_default_preset_is_not_mentioned_to_avoid_noise(tmp_path):
    csv_path = tmp_path / "IVtrace_preset_default_20260101_000000.csv"
    p = _params(); p['preset'] = 'diverging'
    write_results_csv(csv_path, _one_row_df(), p, excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert 'Схема прохода' not in text


def test_turns_other_than_one_are_noted_for_current_excitation(tmp_path):
    csv_path = tmp_path / "IVtrace_turns_20260101_000000.csv"
    p = _params(); p['turns'] = 4.0
    write_results_csv(csv_path, _one_row_df(), p, excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert 'Число витков через датчик: 4.0' in text


def test_default_turns_line_absent(tmp_path):
    csv_path = tmp_path / "IVtrace_turns_default_20260101_000000.csv"
    write_results_csv(csv_path, _one_row_df(), _params(), excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert 'витк' not in text.lower()


def test_turns_not_mentioned_for_voltage_excitation(tmp_path):
    # Витки не имеют смысла для источника напряжения (см. PLAN_V2.md п.37).
    csv_path = tmp_path / "IVtrace_turns_voltage_20260101_000000.csv"
    p = _params(); p['turns'] = 4.0
    write_results_csv(csv_path, _one_row_df(), p, excitation_type='voltage', unit='V')
    text = csv_path.read_text(encoding='utf-8')
    assert 'витк' not in text.lower()


# ----------------------------------------------------------------------
# write_results_csv — метаданные поверки приборов (Ф1 п.3)
# ----------------------------------------------------------------------

def _one_row_df():
    return pd.DataFrame([
        {'Timestamp': '2026-01-01T00:00:00', 'Branch': 'forward', 'X_set': 0.0, 'I_meas_A': 0.0},
    ])


def test_instrument_without_calibration_date_notes_it_is_unspecified(tmp_path):
    csv_path = tmp_path / "IVtrace_cal_unknown_20260101_000000.csv"
    write_results_csv(
        csv_path, _one_row_df(), _params(), excitation_type='current', unit='A',
        instrument_configs=[{'model_name': 'Тестовый мультиметр'}],
    )
    text = csv_path.read_text(encoding='utf-8')
    assert '# Прибор: Тестовый мультиметр (дата поверки не указана в конфиге)\n' in text


def test_instrument_with_valid_calibration_lists_last_and_next_date(tmp_path):
    csv_path = tmp_path / "IVtrace_cal_ok_20260101_000000.csv"
    write_results_csv(
        csv_path, _one_row_df(), _params(), excitation_type='current', unit='A',
        instrument_configs=[{
            'model_name': 'АКИП-1162-10-1020',
            'calibration_date': '2026-01-01',
            'calibration_interval_months': 12,
        }],
    )
    text = csv_path.read_text(encoding='utf-8')
    assert '# Прибор: АКИП-1162-10-1020\n' in text
    assert '#   поверка: 2026-01-01, следующая: 2027-01-01\n' in text
    assert 'ПРОСРОЧЕНА' not in text


def test_overdue_instrument_gets_flagged_in_csv(tmp_path):
    csv_path = tmp_path / "IVtrace_cal_overdue_20260101_000000.csv"
    write_results_csv(
        csv_path, _one_row_df(), _params(), excitation_type='current', unit='A',
        instrument_configs=[{
            'model_name': 'Просроченный прибор',
            'calibration_date': '2020-01-01',
            'calibration_interval_months': 12,
        }],
    )
    text = csv_path.read_text(encoding='utf-8')
    assert 'ПРОСРОЧЕНА' in text


def test_multiple_instruments_each_get_their_own_lines(tmp_path):
    csv_path = tmp_path / "IVtrace_cal_multi_20260101_000000.csv"
    write_results_csv(
        csv_path, _one_row_df(), _params(), excitation_type='current', unit='A',
        instrument_configs=[
            {'model_name': 'Мультиметр'},
            {'model_name': 'Источник'},
        ],
    )
    text = csv_path.read_text(encoding='utf-8')
    assert '# Прибор: Мультиметр (дата поверки не указана в конфиге)\n' in text
    assert '# Прибор: Источник (дата поверки не указана в конфиге)\n' in text


def test_no_instrument_configs_means_no_prib_lines_at_all(tmp_path):
    # Обратная совместимость: старый вызов без instrument_configs не должен
    # писать раздел "# Прибор:" вовсе (см. остальные тесты этого файла).
    csv_path = tmp_path / "IVtrace_cal_none_20260101_000000.csv"
    write_results_csv(csv_path, _one_row_df(), _params(), excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert '# Прибор:' not in text


# ----------------------------------------------------------------------
# _log_calibration_warnings — молчим, пока не станет актуально (Ф1 п.3)
# ----------------------------------------------------------------------

def test_log_stays_silent_for_ok_status():
    lines = []
    _log_calibration_warnings([{
        'model_name': 'X',
        'calibration_date': '2026-01-01',
        'calibration_interval_months': 24,
    }], lines.append)
    assert lines == []


def test_log_notifies_when_no_calibration_data_at_all():
    # UNKNOWN — тоже не молчим: оператор должен узнать, что в конфиге
    # просто нет даты поверки, а не решить, что раз тихо — значит всё ОК.
    lines = []
    _log_calibration_warnings([{'model_name': 'X'}], lines.append)
    assert len(lines) == 1
    assert lines[0].startswith('ℹ')
    assert 'не указана' in lines[0]


def test_log_warns_loudly_when_overdue():
    lines = []
    _log_calibration_warnings([{
        'model_name': 'X',
        'calibration_date': '2020-01-01',
        'calibration_interval_months': 12,
    }], lines.append)
    assert len(lines) == 1
    assert 'ПРОСРОЧЕНА' in lines[0]


def test_log_reports_each_instrument_independently():
    lines = []
    _log_calibration_warnings([
        {'model_name': 'Просрочен', 'calibration_date': '2020-01-01', 'calibration_interval_months': 12},
        {'model_name': 'В порядке', 'calibration_date': '2026-01-01', 'calibration_interval_months': 24},
    ], lines.append)
    assert len(lines) == 1
    assert 'Просрочен' in lines[0]


def test_log_notifies_for_unknown_alongside_overdue_as_separate_lines():
    lines = []
    _log_calibration_warnings([
        {'model_name': 'Без даты'},
        {'model_name': 'Просрочен', 'calibration_date': '2020-01-01', 'calibration_interval_months': 12},
    ], lines.append)
    assert len(lines) == 2
    assert any(l.startswith('ℹ') and 'Без даты' in l for l in lines)
    assert any(l.startswith('⚠') and 'Просрочен' in l for l in lines)
