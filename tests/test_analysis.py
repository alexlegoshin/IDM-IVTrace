import time

import pytest

from analysis import load_and_analyze, find_latest_csv, _read_metadata


def _write_csv(path, extra_header_lines=(), rows=None, excitation_col='X_set'):
    rows = rows if rows is not None else [
        ('forward', 0, 0.0),
        ('forward', 5, 0.005),
        ('reverse', 0, 0.0),
        ('reverse', -5, -0.0051),
    ]
    with open(path, 'w', encoding='utf-8') as f:
        f.write("# Датчик: TestSensor\n")
        for line in extra_header_lines:
            f.write(line + "\n")
        f.write("#\n")
        f.write(f"Timestamp,Branch,{excitation_col},I_meas_A\n")
        for branch, x, i in rows:
            f.write(f"2026-01-01T00:00:00,{branch},{x},{i}\n")


# ----------------------------------------------------------------------
# find_latest_csv
# ----------------------------------------------------------------------

def test_find_latest_csv_returns_most_recently_modified(tmp_path):
    old = tmp_path / "IVtrace_a_20260101_000000.csv"
    new = tmp_path / "IVtrace_b_20260102_000000.csv"
    _write_csv(old)
    time.sleep(0.05)
    _write_csv(new)

    assert find_latest_csv(tmp_path) == new


def test_find_latest_csv_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_latest_csv(tmp_path / "nope")


def test_find_latest_csv_no_matching_files_raises(tmp_path):
    (tmp_path / "unrelated.csv").write_text("x", encoding='utf-8')
    with pytest.raises(FileNotFoundError):
        find_latest_csv(tmp_path)


# ----------------------------------------------------------------------
# _read_metadata
# ----------------------------------------------------------------------

def test_read_metadata_parses_hash_lines(tmp_path):
    csv_path = tmp_path / "IVtrace_x_20260101_000000.csv"
    _write_csv(csv_path, extra_header_lines=[
        "# Тип возбуждения: current",
        "# Единица измерения возбуждения: A",
    ])
    meta = _read_metadata(csv_path)
    assert meta['Датчик'] == 'TestSensor'
    assert meta['Тип возбуждения'] == 'current'
    assert meta['Единица измерения возбуждения'] == 'A'


# ----------------------------------------------------------------------
# load_and_analyze
# ----------------------------------------------------------------------

def test_load_and_analyze_computes_expected_error(tmp_path):
    csv_path = tmp_path / "IVtrace_x_20260101_000000.csv"
    # K = I_meas / X_set идеально линеен -> ожидаем нулевую (или почти нулевую) погрешность.
    _write_csv(csv_path, extra_header_lines=[
        "# Тип возбуждения: current",
        "# Единица измерения возбуждения: A",
    ], rows=[
        ('forward', 0, 0.0),
        ('forward', 10, 0.1),   # K=0.01
        ('reverse', 0, 0.0),
        ('reverse', -10, -0.1),
    ])

    # I_nom=1000 А, X=100 (1:100) -> I_sec_nom = 10 А -> K расчётный = I_sec_nom/I_nom = 0.01,
    # что совпадает с реальным K измерений -> погрешность около нуля.
    stats = load_and_analyze(csv_path, I_nom=1000.0, X=100.0, save_png=True, show=False)

    assert stats['label'] == 'TestSensor'
    assert stats['branches'] == ['forward', 'reverse']
    assert stats['points'] == 4
    assert stats['max_error_percent'] == pytest.approx(0.0, abs=1e-9)
    assert stats['png_path'].exists()


def test_load_and_analyze_detects_nonzero_error(tmp_path):
    csv_path = tmp_path / "IVtrace_x_20260101_000000.csv"
    _write_csv(csv_path, extra_header_lines=[
        "# Тип возбуждения: current",
        "# Единица измерения возбуждения: A",
    ], rows=[
        ('forward', 0, 0.0),
        ('forward', 10, 0.11),  # 10% выше ожидаемого
    ])

    # I_nom=1000, X=100 -> I_sec_nom=10; expected=0.1; measured=0.11 -> |0.01|/10*100 = 0.1%
    stats = load_and_analyze(csv_path, I_nom=1000.0, X=100.0, save_png=False, show=False)
    assert stats['max_error_percent'] == pytest.approx(0.1, abs=1e-9)


def test_load_and_analyze_empty_csv_raises(tmp_path):
    csv_path = tmp_path / "IVtrace_x_20260101_000000.csv"
    _write_csv(csv_path, rows=[])
    with pytest.raises(ValueError):
        load_and_analyze(csv_path, I_nom=100.0, X=10.0)


def test_load_and_analyze_backward_compat_old_i_set_a_column(tmp_path):
    """Старые CSV без X_set (до появления выбора типа возбуждения) должны читаться как ток."""
    csv_path = tmp_path / "IVtrace_old_20260101_000000.csv"
    _write_csv(csv_path, rows=[
        ('forward', 0, 0.0),
        ('forward', 5, 0.05),
    ], excitation_col='I_set_A')

    stats = load_and_analyze(csv_path, I_nom=100.0, X=10.0, save_png=False, show=False)
    assert stats['excitation_type'] == 'current'
    assert stats['excitation_unit'] == 'А'


def test_load_and_analyze_missing_branch_column_defaults_to_forward(tmp_path):
    csv_path = tmp_path / "IVtrace_nobranch_20260101_000000.csv"
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write("# Датчик: OldSensor\n")
        f.write("#\n")
        f.write("Timestamp,X_set,I_meas_A\n")
        f.write("2026-01-01T00:00:00,0,0.0\n")
        f.write("2026-01-01T00:00:00,5,0.05\n")

    stats = load_and_analyze(csv_path, I_nom=100.0, X=10.0, save_png=False, show=False)
    assert stats['branches'] == ['forward']
