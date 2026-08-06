import pandas as pd
import pytest

from orchestrate import (
    write_results_csv, _log_calibration_warnings, _resolve_instruments,
    SessionHandle, ManualControlSession,
)
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


def test_voltage_output_type_gets_beta_note_in_csv(tmp_path):
    csv_path = tmp_path / "IVtrace_out_beta_20260101_000000.csv"
    p = _params(); p['output_type'] = 'voltage'
    write_results_csv(csv_path, _one_row_df(), p, excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert 'BETA' in text


def test_current_output_type_has_no_beta_note(tmp_path):
    csv_path = tmp_path / "IVtrace_out_nobeta_20260101_000000.csv"
    write_results_csv(csv_path, _one_row_df(), _params(), excitation_type='current', unit='A')
    text = csv_path.read_text(encoding='utf-8')
    assert 'BETA' not in text


def test_all_four_excitation_output_combinations_write_independent_headers(tmp_path):
    # п. запроса пользователя: возбуждение и выход датчика — независимые
    # оси, любое сочетание допустимо.
    for excitation_type, output_type in (
        ('current', 'current'), ('current', 'voltage'),
        ('voltage', 'current'), ('voltage', 'voltage'),
    ):
        csv_path = tmp_path / f"IVtrace_combo_{excitation_type}_{output_type}_20260101_000000.csv"
        p = _params(); p['output_type'] = output_type
        write_results_csv(csv_path, _one_row_df(), p, excitation_type=excitation_type,
                          unit='A' if excitation_type == 'current' else 'V')
        meta = _read_metadata(csv_path)
        assert meta['Тип возбуждения'] == excitation_type
        assert meta['Тип выхода датчика'] == output_type


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


# ----------------------------------------------------------------------
# ManualControlSession (Ф4, п.13 — ручное реле, п.40 — прямая уставка)
# ----------------------------------------------------------------------

class _FakeManualSrc:
    def __init__(self):
        self.calls = []
        self.current_setpoints = []
        self.voltage_setpoints = []

    def setup(self, voltage_limit, slew_rate=10.0):
        self.calls.append(('setup', voltage_limit))

    def set_current(self, current):
        self.current_setpoints.append(current)
        self.calls.append(('set_current', current))

    def set_voltage(self, voltage):
        self.voltage_setpoints.append(voltage)
        self.calls.append(('set_voltage', voltage))

    def output_on(self):
        self.calls.append(('output_on',))

    def output_off(self):
        self.calls.append(('output_off',))

    def shutdown(self):
        self.calls.append(('shutdown',))


class _FakeManualRelay:
    def __init__(self):
        self.calls = []

    def forward(self):
        self.calls.append('forward')
        return 'OK'

    def reverse(self):
        self.calls.append('reverse')
        return 'OK'

    def off(self):
        self.calls.append('off')
        return 'OK'


class _FakeManualDmm:
    def __init__(self, readings=None):
        self.readings = list(readings or [])

    def measure(self):
        if not self.readings:
            raise RuntimeError("нет заготовленных показаний")
        return self.readings.pop(0)


def _make_manual_session(excitation_type='current', with_dmm=True):
    handle = SessionHandle()
    handle.src = _FakeManualSrc()
    handle.relay = _FakeManualRelay()
    handle.dmm = _FakeManualDmm([0.1, 0.2]) if with_dmm else None
    return ManualControlSession(handle, excitation_type, log=lambda msg: None), handle


def test_set_relay_forward_reverse_off_call_the_right_relay_method():
    session, handle = _make_manual_session()
    session.set_relay('forward')
    session.set_relay('reverse')
    session.set_relay('off')
    assert handle.relay.calls == ['forward', 'reverse', 'off']


def test_set_relay_rejects_unknown_direction():
    session, _ = _make_manual_session()
    with pytest.raises(ValueError):
        session.set_relay('sideways')


def test_apply_setpoint_positive_value_drives_forward_and_sets_current():
    session, handle = _make_manual_session('current')
    session.apply_setpoint(5.0)
    assert handle.relay.calls == ['forward']
    assert handle.src.current_setpoints == [5.0]
    assert ('output_on',) in handle.src.calls


def test_apply_setpoint_negative_value_drives_reverse_with_positive_magnitude():
    session, handle = _make_manual_session('current')
    session.apply_setpoint(-5.0)
    assert handle.relay.calls == ['reverse']
    assert handle.src.current_setpoints == [5.0]  # магнитуда, не -5.0 — знак кодирует реле, не уставка


def test_apply_setpoint_voltage_excitation_calls_set_voltage():
    session, handle = _make_manual_session('voltage')
    session.apply_setpoint(12.0)
    assert handle.src.voltage_setpoints == [12.0]


def test_apply_setpoint_zero_stops_instead_of_touching_relay():
    # Симметрично точке X=0 в измерительном цикле: реле не трогается ради
    # нулевого сигнала (см. measurement._measure_zero_row).
    session, handle = _make_manual_session()
    session.apply_setpoint(0.0)
    assert handle.relay.calls == ['off']
    assert ('shutdown',) in handle.src.calls


def test_apply_setpoint_does_not_reswitch_relay_when_direction_unchanged():
    session, handle = _make_manual_session()
    session.apply_setpoint(5.0)
    session.apply_setpoint(7.0)
    assert handle.relay.calls == ['forward']  # реле переключено только один раз
    assert handle.src.current_setpoints == [5.0, 7.0]


def test_apply_setpoint_switches_relay_when_sign_flips():
    session, handle = _make_manual_session()
    session.apply_setpoint(5.0)
    session.apply_setpoint(-3.0)
    assert handle.relay.calls == ['forward', 'reverse']


def test_stop_zeroes_source_and_opens_relay():
    session, handle = _make_manual_session()
    session.apply_setpoint(5.0)
    session.stop()
    assert ('shutdown',) in handle.src.calls
    assert handle.relay.calls[-1] == 'off'


def test_read_returns_dmm_measurement_when_dmm_present():
    session, _ = _make_manual_session(with_dmm=True)
    assert session.read() == 0.1
    assert session.read() == 0.2


def test_read_returns_none_when_dmm_absent():
    session, _ = _make_manual_session(with_dmm=False)
    assert session.read() is None


def test_read_returns_none_instead_of_raising_on_dmm_error():
    session, handle = _make_manual_session(with_dmm=True)
    handle.dmm.readings = []  # следующий measure() бросит исключение
    assert session.read() is None


def test_emergency_stop_delegates_to_session_handle():
    session, handle = _make_manual_session()
    calls = []
    handle.emergency_stop = lambda log=None: calls.append(log) or ["выключено"]
    result = session.emergency_stop()
    assert result == ["выключено"]
    assert calls == [None]


def test_close_closes_every_open_instrument():
    session, handle = _make_manual_session(with_dmm=True)
    closed = []
    handle.dmm.close = lambda: closed.append('dmm')
    handle.src.close = lambda: closed.append('src')
    handle.relay.close = lambda: closed.append('relay')
    session.close()
    assert set(closed) == {'dmm', 'src', 'relay'}


def test_close_tolerates_missing_dmm():
    session, handle = _make_manual_session(with_dmm=False)
    handle.src.close = lambda: None
    handle.relay.close = lambda: None
    session.close()  # не должно бросить исключение из-за handle.dmm is None
