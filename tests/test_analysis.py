import time

import pandas as pd
import pytest

from analysis import (
    load_and_analyze, find_latest_csv, _read_metadata,
    load_and_analyze_from_params, metadata_i_nom_and_ratio,
    estimate_ratio_from_data, export_xlsx,
    save_dataframe_with_metadata, apply_invert_input,
)


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


def test_load_and_analyze_excludes_rejected_points_from_stats_but_keeps_them_in_dataframe(tmp_path):
    # Забракованные контрольными промерами точки (п.9, measurement.py)
    # остаются в сырых данных, но не должны портить сводную погрешность.
    csv_path = tmp_path / "IVtrace_rejected_20260101_000000.csv"
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write("# Датчик: TestSensor\n")
        f.write("# Тип возбуждения: current\n")
        f.write("# Единица измерения возбуждения: A\n")
        f.write("#\n")
        f.write("Timestamp,Branch,X_set,I_meas_A,Rejected\n")
        f.write("2026-01-01T00:00:00,forward,0,0.0,False\n")
        f.write("2026-01-01T00:00:00,forward,10,0.1,False\n")   # K=0.01, точно ожидаемое
        f.write("2026-01-01T00:00:00,forward,20,999.0,True\n")  # заведомо бракованное показание

    stats = load_and_analyze(csv_path, I_nom=1000.0, X=100.0, save_png=False, show=False)

    assert stats['points'] == 3          # сырые данные — все три строки
    assert stats['rejected_points'] == 1
    # Без исключения брака max_error_percent улетел бы в тысячи процентов.
    assert stats['max_error_percent'] == pytest.approx(0.0, abs=1e-9)
    assert len(stats['dataframe']) == 3  # точка осталась в df целиком, не удалена


def test_load_and_analyze_without_rejected_column_uses_all_points(tmp_path):
    # Обратная совместимость: CSV до Ф2 без колонки Rejected — все точки участвуют.
    csv_path = tmp_path / "IVtrace_no_rejected_20260101_000000.csv"
    _write_csv(csv_path, extra_header_lines=[
        "# Тип возбуждения: current",
        "# Единица измерения возбуждения: A",
    ], rows=[('forward', 0, 0.0), ('forward', 10, 0.1)])

    stats = load_and_analyze(csv_path, I_nom=1000.0, X=100.0, save_png=False, show=False)
    assert stats['rejected_points'] == 0
    assert stats['points'] == 2


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


# ----------------------------------------------------------------------
# п.31 — знаковая погрешность
# ----------------------------------------------------------------------

def test_error_percent_keeps_sign_of_deviation(tmp_path):
    csv_path = tmp_path / "IVtrace_signed_20260101_000000.csv"
    _write_csv(csv_path, extra_header_lines=[
        "# Тип возбуждения: current",
        "# Единица измерения возбуждения: A",
    ], rows=[
        ('forward', 0, 0.0),
        ('forward', 10, 0.09),   # ниже ожидаемого (0.1) -> отрицательная погрешность
    ])
    stats = load_and_analyze(csv_path, I_nom=1000.0, X=100.0, save_png=False, show=False)
    # measured < expected -> знак минус, не abs().
    assert stats['max_error_percent'] == pytest.approx(-0.1, abs=1e-9)
    assert (stats['dataframe']['Error_percent'] < 0).any()


def test_max_error_percent_picks_largest_magnitude_keeping_its_sign(tmp_path):
    csv_path = tmp_path / "IVtrace_signmix_20260101_000000.csv"
    _write_csv(csv_path, extra_header_lines=[
        "# Тип возбуждения: current",
        "# Единица измерения возбуждения: A",
    ], rows=[
        ('forward', 0, 0.0),
        ('forward', 10, 0.1005),   # expected 0.1 -> +0.05% - маленькое положительное отклонение
        ('forward', 20, 0.1),      # expected 0.2 -> (0.1-0.2)/10*100 = -1.0% - крупное отрицательное
    ])
    stats = load_and_analyze(csv_path, I_nom=1000.0, X=100.0, save_png=False, show=False)
    assert stats['max_error_percent'] < 0
    assert abs(stats['max_error_percent']) > 0.5


# ----------------------------------------------------------------------
# п.26 — ManuallyExcluded исключается из статистики так же, как Rejected
# ----------------------------------------------------------------------

def test_manually_excluded_points_removed_from_stats_but_kept_in_dataframe(tmp_path):
    csv_path = tmp_path / "IVtrace_excl_20260101_000000.csv"
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write("# Датчик: TestSensor\n")
        f.write("# Тип возбуждения: current\n")
        f.write("# Единица измерения возбуждения: A\n")
        f.write("#\n")
        f.write("Timestamp,Branch,X_set,I_meas_A,ManuallyExcluded\n")
        f.write("2026-01-01T00:00:00,forward,0,0.0,False\n")
        f.write("2026-01-01T00:00:00,forward,10,0.1,False\n")
        f.write("2026-01-01T00:00:00,forward,20,999.0,True\n")

    stats = load_and_analyze(csv_path, I_nom=1000.0, X=100.0, save_png=False, show=False)
    assert stats['points'] == 3
    assert stats['rejected_points'] == 1
    assert stats['max_error_percent'] == pytest.approx(0.0, abs=1e-9)
    assert len(stats['dataframe']) == 3


# ----------------------------------------------------------------------
# п.30 — подписи погрешности (смоук: не падает, PNG создаётся)
# ----------------------------------------------------------------------

def test_show_error_labels_does_not_crash_and_saves_png(tmp_path):
    csv_path = tmp_path / "IVtrace_labels_20260101_000000.csv"
    _write_csv(csv_path)
    stats = load_and_analyze(csv_path, I_nom=100.0, X=10.0, save_png=True, show=False,
                             show_error_labels=True)
    assert stats['png_path'].exists()


# ----------------------------------------------------------------------
# п.36 — диапазоны осей (смоук)
# ----------------------------------------------------------------------

def test_axis_limits_do_not_crash_and_save_png(tmp_path):
    csv_path = tmp_path / "IVtrace_lims_20260101_000000.csv"
    _write_csv(csv_path)
    stats = load_and_analyze(csv_path, I_nom=100.0, X=10.0, save_png=True, show=False,
                             xlim=(-6, 6), y1lim=(-0.01, 0.01), y2lim=(-5, 5))
    assert stats['png_path'].exists()


# ----------------------------------------------------------------------
# load_and_analyze_from_params (п.22 — автопостроение по завершении цикла)
# ----------------------------------------------------------------------

def test_load_and_analyze_from_params_returns_none_without_inom_or_ratio(tmp_path):
    csv_path = tmp_path / "IVtrace_noparam_20260101_000000.csv"
    _write_csv(csv_path)
    assert load_and_analyze_from_params(csv_path, {}, save_png=False, show=False) is None
    assert load_and_analyze_from_params(csv_path, {'I_nom': 100.0}, save_png=False, show=False) is None
    assert load_and_analyze_from_params(csv_path, {'ratio': 10.0}, save_png=False, show=False) is None


def test_load_and_analyze_from_params_builds_stats_when_both_present(tmp_path):
    csv_path = tmp_path / "IVtrace_bothparam_20260101_000000.csv"
    _write_csv(csv_path)
    stats = load_and_analyze_from_params(csv_path, {'I_nom': 100.0, 'ratio': 10.0},
                                         save_png=False, show=False)
    assert stats is not None
    assert stats['I_nom'] == 100.0
    assert stats['X'] == 10.0


# ----------------------------------------------------------------------
# metadata_i_nom_and_ratio (п.20 — предзаполнение при открытии произвольного файла)
# ----------------------------------------------------------------------

def test_metadata_i_nom_and_ratio_parses_written_header(tmp_path):
    csv_path = tmp_path / "IVtrace_meta_20260101_000000.csv"
    _write_csv(csv_path, extra_header_lines=[
        "# Номинальный первичный ток: 150.0 А",
        "# Коэффициент преобразования 1:1500.0",
    ])
    I_nom, X = metadata_i_nom_and_ratio(csv_path)
    assert I_nom == 150.0
    assert X == 1500.0


def test_metadata_i_nom_and_ratio_missing_returns_none_none(tmp_path):
    csv_path = tmp_path / "IVtrace_nometa_20260101_000000.csv"
    _write_csv(csv_path)
    I_nom, X = metadata_i_nom_and_ratio(csv_path)
    assert I_nom is None
    assert X is None


# ----------------------------------------------------------------------
# estimate_ratio_from_data (п.10, BETA)
# ----------------------------------------------------------------------

def test_estimate_ratio_from_data_recovers_known_ratio():
    # I_meas = X_set / 1500, точно без шума -> МНК должен вернуть ровно 1500.
    df = pd.DataFrame({
        'X_set': [0.0, 150.0, 300.0, 750.0, 1500.0],
        'I_meas_A': [0.0, 0.1, 0.2, 0.5, 1.0],
    })
    result = estimate_ratio_from_data(df)
    assert result['X_actual'] == pytest.approx(1500.0, rel=1e-6)
    assert result['X_rounded'] == pytest.approx(1500.0)
    assert result['discrepancy_percent'] == pytest.approx(0.0, abs=1e-6)


def test_estimate_ratio_from_data_rounds_to_nearest_multiple_of_50():
    # I_meas = X_set / 1523 -> фактический коэффициент не кратен 50.
    df = pd.DataFrame({
        'X_set': [0.0, 1000.0],
        'I_meas_A': [0.0, 1000.0 / 1523.0],
    })
    result = estimate_ratio_from_data(df)
    assert result['X_actual'] == pytest.approx(1523.0, rel=1e-3)
    assert result['X_rounded'] == 1500.0
    assert result['discrepancy_percent'] > 0


def test_estimate_ratio_from_data_excludes_rejected_and_manually_excluded_rows():
    df = pd.DataFrame({
        'X_set': [0.0, 150.0, 300.0],
        'I_meas_A': [0.0, 0.1, 999.0],       # третья строка - явный выброс
        'Rejected': [False, False, True],
    })
    result = estimate_ratio_from_data(df)
    assert result['X_actual'] == pytest.approx(1500.0, rel=1e-6)


def test_estimate_ratio_from_data_raises_when_all_points_at_zero():
    df = pd.DataFrame({'X_set': [0.0, 0.0], 'I_meas_A': [0.0, 0.0]})
    with pytest.raises(ValueError):
        estimate_ratio_from_data(df)


# ----------------------------------------------------------------------
# export_xlsx (п.21)
# ----------------------------------------------------------------------

def test_export_xlsx_creates_file_with_data_and_metadata_sheets(tmp_path):
    from openpyxl import load_workbook

    csv_path = tmp_path / "IVtrace_xlsx_20260101_000000.csv"
    _write_csv(csv_path, extra_header_lines=["# Тип возбуждения: current"])

    xlsx_path = export_xlsx(csv_path)
    assert xlsx_path == csv_path.with_suffix('.xlsx')
    assert xlsx_path.exists()

    wb = load_workbook(xlsx_path)
    assert "Данные" in wb.sheetnames
    assert "Метаданные" in wb.sheetnames
    ws = wb["Данные"]
    assert ws['A1'].value == 'Timestamp'
    assert ws.freeze_panes == "A2"

    ws_meta = wb["Метаданные"]
    meta_values = [row[0].value for row in ws_meta.iter_rows(min_row=2)]
    assert 'Датчик' in meta_values


def test_export_xlsx_accepts_explicit_output_path(tmp_path):
    csv_path = tmp_path / "IVtrace_xlsx2_20260101_000000.csv"
    _write_csv(csv_path)
    custom_path = tmp_path / "custom_name.xlsx"

    result = export_xlsx(csv_path, xlsx_path=custom_path)
    assert result == custom_path
    assert custom_path.exists()


# ----------------------------------------------------------------------
# save_dataframe_with_metadata (п.26 — сохранение исключений точек)
# ----------------------------------------------------------------------

def test_save_dataframe_with_metadata_preserves_header_and_updates_data(tmp_path):
    csv_path = tmp_path / "IVtrace_save_20260101_000000.csv"
    _write_csv(csv_path, extra_header_lines=["# Тип возбуждения: current"])

    df = pd.read_csv(csv_path, comment='#')
    df['ManuallyExcluded'] = [False] * len(df)
    df.loc[0, 'ManuallyExcluded'] = True
    save_dataframe_with_metadata(csv_path, df)

    metadata = _read_metadata(csv_path)
    assert metadata['Датчик'] == 'TestSensor'
    assert metadata['Тип возбуждения'] == 'current'

    reloaded = pd.read_csv(csv_path, comment='#')
    assert reloaded.loc[0, 'ManuallyExcluded'] == True
    assert len(reloaded) == len(df)


# ----------------------------------------------------------------------
# apply_invert_input (п.26 — пост-обработка, перенесено из Ф0)
# ----------------------------------------------------------------------

def test_apply_invert_input_flips_x_set_sign_in_new_file(tmp_path):
    csv_path = tmp_path / "IVtrace_inv_20260101_000000.csv"
    _write_csv(csv_path, rows=[('forward', 0, 0.0), ('forward', 5, 0.05), ('reverse', -5, -0.05)])

    output_path = apply_invert_input(csv_path)

    assert output_path == csv_path.with_name(csv_path.stem + "_inverted.csv")
    assert output_path.exists()
    assert csv_path.exists()  # исходный файл не тронут

    original = pd.read_csv(csv_path, comment='#')
    inverted = pd.read_csv(output_path, comment='#')
    assert list(inverted['X_set']) == [-v for v in original['X_set']]

    metadata = _read_metadata(output_path)
    assert 'invert_input' in metadata.get('Пост-обработка', '')


def test_apply_invert_input_accepts_explicit_output_path(tmp_path):
    csv_path = tmp_path / "IVtrace_inv2_20260101_000000.csv"
    _write_csv(csv_path)
    custom_path = tmp_path / "custom_inverted.csv"

    result = apply_invert_input(csv_path, output_path=custom_path)
    assert result == custom_path
    assert custom_path.exists()
