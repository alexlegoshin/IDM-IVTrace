from datetime import date

from calibration import CalibrationStatus, WARNING_WINDOW_DAYS, check_calibration


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
