"""
Учёт поверки приборов.

Дата последней поверки и межповерочный интервал хранятся прямо в
JSON-конфиге прибора — "calibration_date" (ISO YYYY-MM-DD) и
"calibration_interval_months" (целое число месяцев). Оба поля
необязательны, и это осознанный выбор: отсутствие данных в конфиге — это
"неизвестно", а не "поверка просрочена". Ни один конфиг в репозитории сейчас
не содержит настоящих дат поверки — их должен внести оператор из реальных
свидетельств о поверке; ничего не выдумывается автоматически.

Проверка не блокирует измерение ни при каком статусе (в отличие от жёсткого
лимита тока реле в limits.py) — просроченная поверка означает, что
результату нельзя доверять как метрологически точному, но это решение
оператора, а не то, что программа вправе принять за него. См. PLAN_V2.md,
п. 3.
"""
import calendar
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional

# "менее 3 месяцев" по ТЗ.
WARNING_WINDOW_DAYS = 90


class CalibrationStatus(Enum):
    UNKNOWN = "unknown"      # в конфиге нет даты/интервала вовсе
    OK = "ok"
    DUE_SOON = "due_soon"    # до окончания поверки меньше WARNING_WINDOW_DAYS
    OVERDUE = "overdue"


@dataclass(frozen=True)
class CalibrationInfo:
    status: CalibrationStatus
    model_name: str
    last_date: Optional[date]
    next_date: Optional[date]
    days_remaining: Optional[int]  # отрицательное — просрочено на столько дней
    message: str


def _add_months(d: date, months: int) -> date:
    """Прибавляет месяцы к дате, корректно перенося год и укорачивая день до конца целевого месяца."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def check_calibration(config: dict, today: Optional[date] = None) -> CalibrationInfo:
    """
    Определяет статус поверки прибора по его конфигу.

    config — словарь конфига прибора (Multimeter.config / CurrentSource.config
    / VoltageSource.config — то, что уже загружено из JSON, дополнительных
    обращений к диску не требуется).
    """
    today = today or date.today()
    model_name = config.get('model_name', 'прибор')
    raw_date = config.get('calibration_date')
    interval_months = config.get('calibration_interval_months')

    if not raw_date or not interval_months:
        return CalibrationInfo(
            status=CalibrationStatus.UNKNOWN,
            model_name=model_name, last_date=None, next_date=None, days_remaining=None,
            message=f"{model_name}: дата поверки не указана в конфиге.",
        )

    last_date = date.fromisoformat(raw_date)
    next_date = _add_months(last_date, interval_months)
    days_remaining = (next_date - today).days

    if days_remaining < 0:
        status = CalibrationStatus.OVERDUE
        message = (
            f"{model_name}: ПОВЕРКА ПРОСРОЧЕНА на {-days_remaining} дн. "
            f"(последняя — {last_date.isoformat()}, требовалась до {next_date.isoformat()}). "
            f"Результатам этого прибора нельзя доверять как метрологически точным."
        )
    elif days_remaining < WARNING_WINDOW_DAYS:
        status = CalibrationStatus.DUE_SOON
        message = (
            f"{model_name}: до окончания срока поверки {days_remaining} дн. "
            f"(следующая поверка — {next_date.isoformat()})."
        )
    else:
        status = CalibrationStatus.OK
        message = f"{model_name}: поверка действительна до {next_date.isoformat()}."

    return CalibrationInfo(
        status=status, model_name=model_name,
        last_date=last_date, next_date=next_date, days_remaining=days_remaining,
        message=message,
    )
