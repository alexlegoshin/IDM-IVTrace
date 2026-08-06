import json
import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from apppaths import APP_VERSION


class ConfigManager:
    """
    Отвечает за чтение/запись JSON-конфига с параметрами последнего измерения
    (excitation_type, X_start, X_stop, X_step, V_limit, delay, cooling_delay,
    label, I_nom, ratio, turns, error_threshold, branch, preset,
    averaging_count, averaging_delay, discard_first, stop_on_error).
    branch (см. sweep.Branch) заменяет собой старое булево use_relay —
    'both' снимает обе полярности автоматически через плату реле в рамках
    одного запуска measure, 'positive'/'negative' — только одну.

    Ничего не знает про приборы — просто key-value хранилище на диске.
    """

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"Предупреждение: не удалось прочитать конфиг ({e}), используются значения по умолчанию.")
        return {}

    def save(self, config: dict):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4, ensure_ascii=False)


#: Только буквы (включая кириллицу), цифры, пробел, дефис и подчёркивание.
#: Никаких точек и разделителей пути вовсе — это не чёрный список опасных
#: символов, а белый список безопасных: обойти его последовательностью вроде
#: "../../.." структурно невозможно, там просто нет символов "." и "/"/"\\".
_SAFE_SENSOR_NAME_RE = re.compile(r'^[\w\- ]+$', re.UNICODE)

_EXCITATION_SUBDIRS = ('current', 'voltage')


class SensorConfigManager:
    """
    Профили датчиков — сохранённые целиком режимы промера (ratio, I_nom,
    turns, диапазон, шаг, задержки, усреднение, направление — см.
    PLAN_V2.md п.39). Один профиль = один датчик и его режим измерения.

    Хранятся ВНЕ рабочей папки с результатами (см. apppaths.sensor_config_dir,
    В-1) — рабочую папку оператор может перенастроить или засорить CSV,
    профили датчиков от этого не должны зависеть и случайно теряться.

    Разложены по подпапкам current/voltage: профиль датчика напряжения
    физически неприменим к промеру током (другие шкалы, ratio в других
    единицах, витки не имеют смысла) — раздельные каталоги делают это
    структурным фактом, а не соглашением, которое можно нарушить.
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.subdirs = {}
        for excitation_type in _EXCITATION_SUBDIRS:
            d = self.base_dir / excitation_type
            d.mkdir(parents=True, exist_ok=True)
            self.subdirs[excitation_type] = d

    def _safe_filename(self, name: str) -> str:
        name = name.strip()
        if not name:
            raise ValueError("Имя конфига не может быть пустым.")
        if not _SAFE_SENSOR_NAME_RE.match(name):
            raise ValueError(
                f"Недопустимое имя конфига: {name!r}. "
                "Разрешены буквы, цифры, пробел, дефис и подчёркивание "
                "(без точек и разделителей пути)."
            )
        return name + '.json'

    def _subdir(self, excitation_type: str) -> Path:
        if excitation_type not in self.subdirs:
            raise ValueError(
                f"Неизвестный тип возбуждения: {excitation_type!r} "
                f"(ожидается одно из {_EXCITATION_SUBDIRS})"
            )
        return self.subdirs[excitation_type]

    def save_sensor_config(self, name: str, params: dict,
                           excitation_type: Optional[str] = None,
                           comment: str = '') -> Path:
        """
        Сохраняет режим измерения датчика в JSON-файл.

        excitation_type — в какую подпапку класть; если не передан явно,
        берётся из params['excitation_type'] (там он обычно уже есть —
        params это то же самое, что уходит в ConfigManager).

        Профиль дополняется блоком "_meta" (когда сохранён, каким билдом,
        комментарий оператора) — без этого через полгода не разобрать, что
        за файл, см. п.39.
        """
        excitation_type = excitation_type or params.get('excitation_type')
        filename = self._safe_filename(name)
        path = self._subdir(excitation_type) / filename

        payload = dict(params)
        payload['_meta'] = {
            'saved_at': datetime.now().isoformat(timespec='seconds'),
            'app_version': APP_VERSION,
            'comment': comment,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        return path

    def load_sensor_config(self, name: str, excitation_type: Optional[str] = None) -> Optional[dict]:
        """
        Загружает профиль датчика.

        excitation_type сужает поиск до одной подпапки. Без него ищем в
        обеих — нужно для CLI-флага --load-config: на момент загрузки тип
        возбуждения ещё может быть не определён (он и сам иногда приходит
        ИЗ загружаемого профиля, см. cli.resolve_measure_params). Если
        имя совпадает в обеих подпапках, побеждает 'current' — это редкий
        случай (оператор сам выбирает уникальные имена), для однозначности
        в GUI excitation_type всегда передаётся явно.
        """
        try:
            filename = self._safe_filename(name)
        except ValueError as e:
            print(f"Предупреждение: {e}")
            return None

        dirs = [self._subdir(excitation_type)] if excitation_type else list(self.subdirs.values())
        for d in dirs:
            path = d / filename
            if not path.exists():
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"Предупреждение: не удалось прочитать конфиг датчика ({e})")
                return None
        return None

    def list_sensor_configs(self, excitation_type: Optional[str] = None) -> List[str]:
        """Имена профилей (без расширения) — для excitation_type или для обоих типов сразу."""
        dirs = [self._subdir(excitation_type)] if excitation_type else list(self.subdirs.values())
        names = set()
        for d in dirs:
            names.update(p.stem for p in d.glob('*.json'))
        return sorted(names)

    def rename_sensor_config(self, old_name: str, new_name: str,
                             excitation_type: Optional[str] = None) -> bool:
        """
        Переименовывает профиль (п.39-UI). Содержимое (включая _meta с
        исходным saved_at/comment) переносится как есть — переименование не
        то же самое, что новое сохранение, дата создания профиля не должна
        подменяться датой переименования.

        Возвращает False, если исходного профиля не нашлось (нечего
        переименовывать) — как и delete_sensor_config, не бросает исключение
        на отсутствующем файле.
        """
        params = self.load_sensor_config(old_name, excitation_type=excitation_type)
        if params is None:
            return False
        actual_excitation = excitation_type or params.get('excitation_type')

        filename = self._safe_filename(new_name)
        path = self._subdir(actual_excitation) / filename
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(params, f, indent=4, ensure_ascii=False)

        self.delete_sensor_config(old_name, excitation_type=actual_excitation)
        return True

    def delete_sensor_config(self, name: str, excitation_type: Optional[str] = None) -> bool:
        """Удаляет профиль. Без excitation_type удаляет из обеих подпапок (на случай совпадения имён)."""
        try:
            filename = self._safe_filename(name)
        except ValueError as e:
            print(f"Предупреждение: {e}")
            return False

        dirs = [self._subdir(excitation_type)] if excitation_type else list(self.subdirs.values())
        deleted = False
        for d in dirs:
            path = d / filename
            if path.exists():
                path.unlink()
                deleted = True
        return deleted
