import json
from datetime import date

import pytest

from calibration import (
    CalibrationStatus, WARNING_WINDOW_DAYS, check_calibration,
    list_instrument_configs, update_calibration_date,
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
# list_instrument_configs (п.3-UI)
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
# update_calibration_date (п.3-UI)
# ----------------------------------------------------------------------

def test_update_calibration_date_writes_fields_and_preserves_others(tmp_path):
    cfg_path = tmp_path / "instr.json"
    cfg_path.write_text(json.dumps({'model_name': 'X', 'keywords': ['X']}, ensure_ascii=False), encoding='utf-8')

    update_calibration_date(cfg_path, '2026-01-15', 12)

    data = json.loads(cfg_path.read_text(encoding='utf-8'))
    assert data['calibration_date'] == '2026-01-15'
    assert data['calibration_interval_months'] == 12
    assert data['model_name'] == 'X'
    assert data['keywords'] == ['X']


def test_update_calibration_date_result_is_readable_by_check_calibration(tmp_path):
    cfg_path = tmp_path / "instr.json"
    cfg_path.write_text(json.dumps({'model_name': 'X'}), encoding='utf-8')
    update_calibration_date(cfg_path, '2026-01-01', 12)

    data = json.loads(cfg_path.read_text(encoding='utf-8'))
    info = check_calibration(data, today=date(2026, 6, 1))
    assert info.status == CalibrationStatus.OK


def test_update_calibration_date_rejects_malformed_date(tmp_path):
    cfg_path = tmp_path / "instr.json"
    cfg_path.write_text(json.dumps({'model_name': 'X'}), encoding='utf-8')
    with pytest.raises(ValueError):
        update_calibration_date(cfg_path, 'not-a-date', 12)


def test_update_calibration_date_rejects_nonpositive_interval(tmp_path):
    cfg_path = tmp_path / "instr.json"
    cfg_path.write_text(json.dumps({'model_name': 'X'}), encoding='utf-8')
    with pytest.raises(ValueError):
        update_calibration_date(cfg_path, '2026-01-01', 0)


def test_update_calibration_date_overwrites_previous_value(tmp_path):
    cfg_path = tmp_path / "instr.json"
    cfg_path.write_text(json.dumps({
        'model_name': 'X', 'calibration_date': '2020-01-01', 'calibration_interval_months': 6,
    }), encoding='utf-8')

    update_calibration_date(cfg_path, '2026-03-01', 24)

    data = json.loads(cfg_path.read_text(encoding='utf-8'))
    assert data['calibration_date'] == '2026-03-01'
    assert data['calibration_interval_months'] == 24
