import pandas as pd

from orchestrate import write_results_csv
from analysis import _read_metadata, load_and_analyze


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
