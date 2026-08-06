"""
Учёт поверки приборов — реестр ФИЗИЧЕСКИХ приборов, отдельно от конфигов
модели (instruments/*/*.json).

Баг-репорт (см. PLAN_V2.md): раньше дата поверки жила прямо в конфиге
модели. Это ломалось дважды:
  1. один физический прибор (например АКИП-2101) описан ДВУМЯ конфигами —
     амперметровым (multimeters_current/) и вольтметровым
     (multimeters_voltage/) — с датой поверки в файле она бы дублировалась
     и могла разойтись;
  2. если в лаборатории появится ВТОРОЙ физический экземпляр той же модели,
     оба конфига-файла модели — ОДНИ на оба прибора, разные даты поверки
     для двух юнитов в файл модели не поместить.

Решение: конфиг модели (instruments/*/*.json) описывает ПРОТОКОЛ (команды,
диапазоны, keywords) и несёт `model_id` — общий у current/voltage-вариантов
одной физической модели. Поверка живёт в ОТДЕЛЬНОМ реестре
(instrument_registry.json, см. apppaths.config_dir) — список записей
{model_id, serial_number, calibration_date, calibration_interval_months, ...},
по одной на каждый физический экземпляр. serial_number == "" значит
"единственный экземпляр этой модели" (не привязываем к S/N, если он и так
один — вводить серийный номер вручную для этого случая не обязательно).

Ограничение, которое НЕ решено и не может быть решено этой правкой:
автообнаружение (*IDN?) не даёт надёжно вытащить серийный номер — формат
ответа у каждого вендора свой (см. orchestrate.py). Поэтому если для одной
модели в реестре ЗАВЕДЕНО больше одного прибора, программа не может сама
понять, какой из них сейчас подключён — это CalibrationStatus.AMBIGUOUS,
явный статус "не могу определить", а не тихое угадывание.

Проверка поверки не блокирует измерение ни при каком статусе (в отличие от
жёсткого лимита тока реле в limits.py) — просроченная поверка означает, что
результату нельзя доверять как метрологически точному, но это решение
оператора, а не то, что программа вправе принять за него. См. PLAN_V2.md, п. 3.
"""
import calendar
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from enum import Enum
from typing import List, Optional

# "менее 3 месяцев" по ТЗ.
WARNING_WINDOW_DAYS = 90


class CalibrationStatus(Enum):
    UNKNOWN = "unknown"        # нет записи в реестре / нет даты в записи
    OK = "ok"
    DUE_SOON = "due_soon"      # до окончания поверки меньше WARNING_WINDOW_DAYS
    OVERDUE = "overdue"
    AMBIGUOUS = "ambiguous"    # >1 прибора этой модели в реестре, *IDN? не различает


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


def check_calibration(fields: dict, today: Optional[date] = None) -> CalibrationInfo:
    """
    Определяет статус поверки по плоскому словарю с тремя ключами:
    'model_name' (опционально), 'calibration_date' (ISO YYYY-MM-DD или None),
    'calibration_interval_months' (int или None). Не привязана к тому,
    откуда взят этот словарь — конфиг модели (легаси) или запись реестра
    (InstrumentRecord.to_dict-подобная форма) — обе несут одни и те же три
    ключа под одними именами.
    """
    today = today or date.today()
    model_name = fields.get('model_name') or 'прибор'
    raw_date = fields.get('calibration_date')
    interval_months = fields.get('calibration_interval_months')

    if not raw_date or not interval_months:
        return CalibrationInfo(
            status=CalibrationStatus.UNKNOWN,
            model_name=model_name, last_date=None, next_date=None, days_remaining=None,
            message=f"{model_name}: дата поверки не указана.",
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


def list_instrument_configs(config_dirs) -> list:
    """
    Все json-конфиги МОДЕЛЕЙ из перечисленных каталогов (см. apppaths:
    multimeter_cfg_dir/voltmeter_cfg_dir/current_source_cfg_dir/
    voltage_source_cfg_dir) — источник списка известных моделей для
    редактора поверки (список физических приборов — list_calibration_rows).
    """
    found = []
    for d in config_dirs:
        found.extend(sorted(Path(d).glob('*.json')))
    return found


def known_models(config_dirs) -> dict:
    """{model_id: model_name} по первому встреченному конфигу с этим model_id."""
    models = {}
    for p in list_instrument_configs(config_dirs):
        try:
            cfg = json.loads(p.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            continue
        model_id = cfg.get('model_id')
        if model_id and model_id not in models:
            models[model_id] = cfg.get('model_name', model_id)
    return models


# ----------------------------------------------------------------------
# Реестр физических приборов (instrument_registry.json)
# ----------------------------------------------------------------------

@dataclass
class InstrumentRecord:
    model_id: str
    serial_number: str = ""   # "" — единственный экземпляр модели, без привязки к S/N
    label: str = ""           # человекочитаемая пометка ("стол №1"), опционально
    calibration_date: Optional[str] = None
    calibration_interval_months: Optional[int] = None
    comment: str = ""

    def to_dict(self) -> dict:
        return {
            'model_id': self.model_id, 'serial_number': self.serial_number, 'label': self.label,
            'calibration_date': self.calibration_date,
            'calibration_interval_months': self.calibration_interval_months,
            'comment': self.comment,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "InstrumentRecord":
        return cls(
            model_id=d['model_id'], serial_number=d.get('serial_number', ''),
            label=d.get('label', ''), calibration_date=d.get('calibration_date'),
            calibration_interval_months=d.get('calibration_interval_months'),
            comment=d.get('comment', ''),
        )


def registry_path() -> Path:
    from apppaths import config_dir
    return config_dir() / "instrument_registry.json"


def load_registry(path: Optional[Path] = None) -> List[InstrumentRecord]:
    path = Path(path) if path is not None else registry_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []
    return [InstrumentRecord.from_dict(r) for r in data.get('records', [])]


def save_registry(records: List[InstrumentRecord], path: Optional[Path] = None) -> None:
    path = Path(path) if path is not None else registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'records': [r.to_dict() for r in records]}
    path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding='utf-8')


def set_calibration_record(model_id: str, serial_number: str, calibration_date: str,
                            calibration_interval_months: int, label: str = "", comment: str = "",
                            path: Optional[Path] = None) -> None:
    """
    Заводит новую запись реестра или обновляет существующую (ключ —
    пара model_id+serial_number). calibration_date проверяется через
    date.fromisoformat — лучше явная ошибка в UI, чем тихо записанная
    нечитаемая строка.
    """
    date.fromisoformat(calibration_date)  # бросает ValueError на некорректном формате
    if calibration_interval_months <= 0:
        raise ValueError("Межповерочный интервал должен быть положительным числом месяцев.")

    records = load_registry(path)
    for r in records:
        if r.model_id == model_id and r.serial_number == serial_number:
            r.calibration_date = calibration_date
            r.calibration_interval_months = calibration_interval_months
            if label:
                r.label = label
            if comment:
                r.comment = comment
            break
    else:
        records.append(InstrumentRecord(
            model_id=model_id, serial_number=serial_number, label=label,
            calibration_date=calibration_date,
            calibration_interval_months=calibration_interval_months, comment=comment,
        ))
    save_registry(records, path)


def delete_calibration_record(model_id: str, serial_number: str, path: Optional[Path] = None) -> None:
    records = [r for r in load_registry(path)
               if not (r.model_id == model_id and r.serial_number == serial_number)]
    save_registry(records, path)


def resolve_calibration_info(config: dict, records: Optional[List[InstrumentRecord]] = None) -> CalibrationInfo:
    """
    По конфигу МОДЕЛИ (Multimeter.config / CurrentSource.config /
    VoltageSource.config — то, что уже открыто и опрошено при
    автообнаружении) находит статус поверки ФИЗИЧЕСКОГО прибора в реестре.

    - Нет model_id в конфиге (не мигрированный конфиг) -> UNKNOWN.
    - В реестре нет записи для этого model_id -> UNKNOWN ("не заведён").
    - Ровно одна запись -> обычная проверка (см. check_calibration).
    - Больше одной записи -> AMBIGUOUS: по *IDN? нельзя надёжно определить
      серийный номер подключённого прибора, значит нельзя сказать, какая из
      записей относится к нему — не угадываем.
    """
    model_id = config.get('model_id')
    model_name = config.get('model_name') or 'прибор'
    if not model_id:
        return CalibrationInfo(
            status=CalibrationStatus.UNKNOWN, model_name=model_name,
            last_date=None, next_date=None, days_remaining=None,
            message=f"{model_name}: конфиг не привязан к записи реестра (нет model_id).",
        )

    if records is None:
        records = load_registry()
    matches = [r for r in records if r.model_id == model_id]

    if not matches:
        return CalibrationInfo(
            status=CalibrationStatus.UNKNOWN, model_name=model_name,
            last_date=None, next_date=None, days_remaining=None,
            message=f"{model_name}: прибор не заведён в реестре (нет записи о поверке).",
        )

    if len(matches) > 1:
        return CalibrationInfo(
            status=CalibrationStatus.AMBIGUOUS, model_name=model_name,
            last_date=None, next_date=None, days_remaining=None,
            message=(f"{model_name}: в реестре {len(matches)} прибора(ов) этой модели — "
                     "по ответу *IDN? нельзя определить, какой именно подключён. "
                     "Уточните серийный номер в редакторе поверки."),
        )

    record = matches[0]
    return check_calibration({
        'model_name': record.label or model_name,
        'calibration_date': record.calibration_date,
        'calibration_interval_months': record.calibration_interval_months,
    })


def list_calibration_rows(config_dirs, records: Optional[List[InstrumentRecord]] = None) -> List[dict]:
    """
    Одна строка на каждую запись реестра плюс одна строка-заглушка на
    каждую известную модель (по конфигам в config_dirs), у которой пока нет
    ни одной записи — источник строк для редактора поверки (п.3-UI).

    Возвращает список словарей: model_id, serial_number, label, model_name,
    comment, calibration_interval_months (int|None — сырое значение, для
    предзаполнения формы редактора; сам CalibrationInfo его не несёт), info
    (CalibrationInfo), has_record (bool).
    """
    if records is None:
        records = load_registry()
    models = known_models(config_dirs)

    rows = []
    seen_model_ids = set()
    for r in records:
        model_name = models.get(r.model_id, r.model_id)
        info = check_calibration({
            'model_name': r.label or model_name,
            'calibration_date': r.calibration_date,
            'calibration_interval_months': r.calibration_interval_months,
        })
        rows.append({
            'model_id': r.model_id, 'serial_number': r.serial_number, 'label': r.label,
            'model_name': model_name, 'comment': r.comment,
            'calibration_interval_months': r.calibration_interval_months,
            'info': info, 'has_record': True,
        })
        seen_model_ids.add(r.model_id)

    for model_id, model_name in models.items():
        if model_id in seen_model_ids:
            continue
        info = CalibrationInfo(
            status=CalibrationStatus.UNKNOWN, model_name=model_name,
            last_date=None, next_date=None, days_remaining=None,
            message=f"{model_name}: прибор не заведён в реестре.",
        )
        rows.append({
            'model_id': model_id, 'serial_number': '', 'label': '', 'model_name': model_name,
            'comment': '', 'calibration_interval_months': None, 'info': info, 'has_record': False,
        })
    return rows
