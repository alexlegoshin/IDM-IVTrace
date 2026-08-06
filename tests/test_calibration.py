import json
from datetime import date

import pytest

from calibration import (
    CalibrationStatus, WARNING_WINDOW_DAYS, InstrumentRecord,
    check_calibration, list_instrument_configs,
    load_registry, save_registry, set_calibration_record, delete_calibration_record,
    resolve_calibration_info, list_calibration_rows,
)


def _cfg(**overrides):
    cfg = {'model_name': 'Тестовый прибор'}
    cfg.update(overrides)
    return cfg


# ----------------------------------------------------------------------
# UNKNOWN — отсутствие данных не значит "просрочено"
# ----------------------------------------------------------------------

def test_missing_both_fields_is_unknown_not_overdue():
    info = check_calibration(_cfg())
    assert info.status == CalibrationStatus.UNKNOWN
    assert info.last_date is None
    assert info.next_date is None


def test_missing_interval_is_unknown():
    info = check_calibration(_cfg(calibration_date='2026-01-01'))
    assert info.status == CalibrationStatus.UNKNOWN


def test_missing_date_is_unknown():
    info = check_calibration(_cfg(calibration_interval_months=12))
    assert info.status == CalibrationStatus.UNKNOWN


# ----------------------------------------------------------------------
# OK / DUE_SOON / OVERDUE
# ----------------------------------------------------------------------

def test_far_in_the_future_is_ok():
    info = check_calibration(
        _cfg(calibration_date='2026-01-01', calibration_interval_months=12),
        today=date(2026, 2, 1),
    )
    assert info.status == CalibrationStatus.OK
    assert info.next_date == date(2027, 1, 1)


def test_just_inside_warning_window_is_due_soon():
    # Поверка была год назад, интервал 12 месяцев -> следующая ровно сегодня
    # + (WARNING_WINDOW_DAYS - 1), то есть чуть меньше порога.
    last = date(2026, 1, 1)
    next_due = date(2027, 1, 1)
    today = date(2026, 10, 10)  # до next_due заведомо меньше 90 дней
    info = check_calibration(
        _cfg(calibration_date=last.isoformat(), calibration_interval_months=12),
        today=today,
    )
    assert info.status == CalibrationStatus.DUE_SOON
    assert 0 <= info.days_remaining < WARNING_WINDOW_DAYS


def test_exactly_at_warning_window_boundary_is_still_ok():
    # days_remaining == WARNING_WINDOW_DAYS ровно -> порог "< 90", то есть
    # граница ещё OK, не DUE_SOON.
    from datetime import timedelta
    next_due = date(2027, 1, 1)
    today = next_due - timedelta(days=WARNING_WINDOW_DAYS)
    info = check_calibration(
        _cfg(calibration_date='2026-01-01', calibration_interval_months=12),
        today=today,
    )
    assert info.status == CalibrationStatus.OK
    assert info.days_remaining == WARNING_WINDOW_DAYS


def test_overdue_gives_negative_days_remaining_and_loud_message():
    info = check_calibration(
        _cfg(calibration_date='2025-01-01', calibration_interval_months=12),
        today=date(2026, 6, 1),
    )
    assert info.status == CalibrationStatus.OVERDUE
    assert info.days_remaining < 0
    assert 'ПРОСРОЧЕНА' in info.message


def test_overdue_by_exactly_one_day():
    info = check_calibration(
        _cfg(calibration_date='2026-01-01', calibration_interval_months=1),
        today=date(2026, 2, 2),
    )
    assert info.status == CalibrationStatus.OVERDUE
    assert info.days_remaining == -1


def test_due_today_exactly_is_not_yet_overdue():
    info = check_calibration(
        _cfg(calibration_date='2026-01-01', calibration_interval_months=1),
        today=date(2026, 2, 1),
    )
    assert info.status != CalibrationStatus.OVERDUE
    assert info.days_remaining == 0


# ----------------------------------------------------------------------
# _add_months — перенос года и укорачивание дня
# ----------------------------------------------------------------------

def test_add_months_rolls_over_year():
    info = check_calibration(
        _cfg(calibration_date='2026-11-01', calibration_interval_months=3),
        today=date(2027, 1, 1),
    )
    assert info.next_date == date(2027, 2, 1)


def test_add_months_clamps_day_to_shorter_target_month():
    # 31 января + 1 месяц -> 28/29 февраля, не "31 февраля" (несуществующая дата).
    info = check_calibration(
        _cfg(calibration_date='2026-01-31', calibration_interval_months=1),
        today=date(2026, 2, 1),
    )
    assert info.next_date == date(2026, 2, 28)


# ----------------------------------------------------------------------
# model_name попадает в сообщение
# ----------------------------------------------------------------------

def test_model_name_appears_in_message():
    info = check_calibration(_cfg(model_name='АКИП-1162-10-1020'))
    assert 'АКИП-1162-10-1020' in info.message


def test_missing_model_name_falls_back_to_generic_label():
    info = check_calibration({})
    assert info.model_name == 'прибор'


# ----------------------------------------------------------------------
# list_instrument_configs (п.3-UI) — конфиги МОДЕЛЕЙ, не приборов
# ----------------------------------------------------------------------

def test_list_instrument_configs_collects_json_from_all_dirs(tmp_path):
    dir_a = tmp_path / "a"; dir_a.mkdir()
    dir_b = tmp_path / "b"; dir_b.mkdir()
    (dir_a / "one.json").write_text("{}", encoding='utf-8')
    (dir_b / "two.json").write_text("{}", encoding='utf-8')
    (dir_b / "not_json.txt").write_text("x", encoding='utf-8')

    found = list_instrument_configs([dir_a, dir_b])
    names = sorted(p.name for p in found)
    assert names == ["one.json", "two.json"]


def test_list_instrument_configs_empty_dirs_returns_empty_list(tmp_path):
    empty = tmp_path / "empty"; empty.mkdir()
    assert list_instrument_configs([empty]) == []


# ----------------------------------------------------------------------
# Реестр физических приборов (бага 6+7): load_registry/save_registry
# ----------------------------------------------------------------------

def test_load_registry_missing_file_returns_empty_list(tmp_path):
    assert load_registry(tmp_path / "no_such_file.json") == []


def test_save_then_load_registry_roundtrip(tmp_path):
    path = tmp_path / "registry.json"
    records = [
        InstrumentRecord(model_id='akip2101', serial_number='SN1', calibration_date='2026-01-01',
                          calibration_interval_months=12, comment='основной'),
        InstrumentRecord(model_id='akip1162'),
    ]
    save_registry(records, path)
    loaded = load_registry(path)
    assert len(loaded) == 2
    assert loaded[0].model_id == 'akip2101'
    assert loaded[0].serial_number == 'SN1'
    assert loaded[0].comment == 'основной'
    assert loaded[1].serial_number == ''  # дефолт "единственный экземпляр"


def test_load_registry_corrupt_file_returns_empty_list(tmp_path):
    path = tmp_path / "registry.json"
    path.write_text("{not valid json", encoding='utf-8')
    assert load_registry(path) == []


# ----------------------------------------------------------------------
# set_calibration_record / delete_calibration_record
# ----------------------------------------------------------------------

def test_set_calibration_record_creates_new_entry(tmp_path):
    path = tmp_path / "registry.json"
    set_calibration_record('akip2101', '', '2026-01-15', 12, path=path)
    records = load_registry(path)
    assert len(records) == 1
    assert records[0].model_id == 'akip2101'
    assert records[0].calibration_date == '2026-01-15'
    assert records[0].calibration_interval_months == 12


def test_set_calibration_record_updates_existing_entry_by_model_and_serial(tmp_path):
    path = tmp_path / "registry.json"
    set_calibration_record('akip2101', 'SN1', '2020-01-01', 6, path=path)
    set_calibration_record('akip2101', 'SN1', '2026-03-01', 24, path=path)
    records = load_registry(path)
    assert len(records) == 1
    assert records[0].calibration_date == '2026-03-01'
    assert records[0].calibration_interval_months == 24


def test_set_calibration_record_distinguishes_two_units_of_same_model_by_serial(tmp_path):
    path = tmp_path / "registry.json"
    set_calibration_record('akip2101', 'SN1', '2026-01-01', 12, path=path)
    set_calibration_record('akip2101', 'SN2', '2020-01-01', 12, path=path)
    records = load_registry(path)
    assert len(records) == 2
    by_serial = {r.serial_number: r for r in records}
    assert by_serial['SN1'].calibration_date == '2026-01-01'
    assert by_serial['SN2'].calibration_date == '2020-01-01'


def test_set_calibration_record_rejects_malformed_date(tmp_path):
    path = tmp_path / "registry.json"
    with pytest.raises(ValueError):
        set_calibration_record('akip2101', '', 'not-a-date', 12, path=path)


def test_set_calibration_record_rejects_nonpositive_interval(tmp_path):
    path = tmp_path / "registry.json"
    with pytest.raises(ValueError):
        set_calibration_record('akip2101', '', '2026-01-01', 0, path=path)


def test_delete_calibration_record_removes_only_matching_entry(tmp_path):
    path = tmp_path / "registry.json"
    set_calibration_record('akip2101', 'SN1', '2026-01-01', 12, path=path)
    set_calibration_record('akip2101', 'SN2', '2026-01-01', 12, path=path)
    delete_calibration_record('akip2101', 'SN1', path=path)
    records = load_registry(path)
    assert len(records) == 1
    assert records[0].serial_number == 'SN2'


# ----------------------------------------------------------------------
# resolve_calibration_info — конфиг МОДЕЛИ -> статус поверки ПРИБОРА
# ----------------------------------------------------------------------

def test_resolve_calibration_info_no_model_id_is_unknown():
    info = resolve_calibration_info({'model_name': 'X'})
    assert info.status == CalibrationStatus.UNKNOWN
    assert 'model_id' in info.message


def test_resolve_calibration_info_no_matching_record_is_unknown():
    info = resolve_calibration_info({'model_name': 'X', 'model_id': 'x'}, records=[])
    assert info.status == CalibrationStatus.UNKNOWN
    assert 'не заведён' in info.message


def test_resolve_calibration_info_single_record_gives_normal_status():
    records = [InstrumentRecord(model_id='x', calibration_date='2026-01-01',
                                 calibration_interval_months=12)]
    info = resolve_calibration_info({'model_name': 'X', 'model_id': 'x'}, records=records,
                                     )
    assert info.status in (CalibrationStatus.OK, CalibrationStatus.DUE_SOON, CalibrationStatus.OVERDUE)


def test_resolve_calibration_info_two_records_same_model_is_ambiguous():
    records = [
        InstrumentRecord(model_id='dup', serial_number='SN1'),
        InstrumentRecord(model_id='dup', serial_number='SN2'),
    ]
    info = resolve_calibration_info({'model_name': 'X', 'model_id': 'dup'}, records=records)
    assert info.status == CalibrationStatus.AMBIGUOUS
    assert 'нельзя определить' in info.message


def test_resolve_calibration_info_uses_record_label_over_config_model_name_when_set():
    records = [InstrumentRecord(model_id='x', label='Стол №2', calibration_date='2026-01-01',
                                 calibration_interval_months=12)]
    info = resolve_calibration_info({'model_name': 'X (общее имя модели)', 'model_id': 'x'}, records=records)
    assert info.model_name == 'Стол №2'


# ----------------------------------------------------------------------
# list_calibration_rows — сводка для редактора поверки (п.3-UI)
# ----------------------------------------------------------------------

def test_list_calibration_rows_shows_unregistered_model_as_placeholder(tmp_path):
    d = tmp_path / "dmm"; d.mkdir()
    (d / "x.json").write_text(json.dumps({'model_id': 'x', 'model_name': 'X'}), encoding='utf-8')

    rows = list_calibration_rows([d], records=[])
    assert len(rows) == 1
    assert rows[0]['model_id'] == 'x'
    assert rows[0]['has_record'] is False
    assert rows[0]['info'].status == CalibrationStatus.UNKNOWN


def test_list_calibration_rows_shows_every_registry_record_even_for_same_model(tmp_path):
    d = tmp_path / "dmm"; d.mkdir()
    (d / "x.json").write_text(json.dumps({'model_id': 'dup', 'model_name': 'X'}), encoding='utf-8')

    records = [
        InstrumentRecord(model_id='dup', serial_number='SN1', calibration_date='2026-01-01',
                          calibration_interval_months=12),
        InstrumentRecord(model_id='dup', serial_number='SN2'),
    ]
    rows = list_calibration_rows([d], records=records)
    assert len(rows) == 2
    assert all(r['has_record'] for r in rows)
    assert {r['serial_number'] for r in rows} == {'SN1', 'SN2'}


def test_list_calibration_rows_does_not_duplicate_model_shared_by_two_configs(tmp_path):
    # Ровно ситуация бага 6: current/voltage-конфиги одной физической
    # модели делят один model_id -> одна строка-заглушка, не две.
    current_dir = tmp_path / "current"; current_dir.mkdir()
    voltage_dir = tmp_path / "voltage"; voltage_dir.mkdir()
    (current_dir / "akip2101.json").write_text(
        json.dumps({'model_id': 'akip2101', 'model_name': 'AKIP-2101 (амперметр)'}), encoding='utf-8')
    (voltage_dir / "akip2101.json").write_text(
        json.dumps({'model_id': 'akip2101', 'model_name': 'AKIP-2101 (вольтметр)'}), encoding='utf-8')

    rows = list_calibration_rows([current_dir, voltage_dir], records=[])
    assert len(rows) == 1
