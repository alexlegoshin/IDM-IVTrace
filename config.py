import json
from pathlib import Path
from typing import Optional


class ConfigManager:
    """
    Отвечает за чтение/запись JSON-конфига с параметрами последнего измерения
    (excitation_type, X_start, X_stop, X_step, V_limit, delay, cooling_delay,
    label, I_nom, ratio, error_threshold, use_relay, stop_on_error).
    Направление (forward/reverse) не хранится — обе ветви снимаются
    автоматически через плату реле в рамках одного запуска measure.

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


class SensorConfigManager:
    """
    Управление конфигурациями датчиков (сохранение/загрузка отдельных файлов).
    Конфиги хранятся в папке sensor_configs внутри data_dir.
    """

    def __init__(self, data_dir: Path):
        self.configs_dir = data_dir / "sensor_configs"
        self.configs_dir.mkdir(parents=True, exist_ok=True)

    def _ensure_json_ext(self, filename: str) -> str:
        if not filename.endswith('.json'):
            return filename + '.json'
        return filename

    def save_sensor_config(self, filename: str, params: dict) -> Path:
        """Сохраняет конфигурацию датчика в JSON-файл."""
        filename = self._ensure_json_ext(filename)
        path = self.configs_dir / filename
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(params, f, indent=4, ensure_ascii=False)
        return path

    def load_sensor_config(self, filename: str) -> Optional[dict]:
        """Загружает конфигурацию датчика из JSON-файла."""
        filename = self._ensure_json_ext(filename)
        path = self.configs_dir / filename
        if not path.exists():
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Предупреждение: не удалось прочитать конфиг датчика ({e})")
            return None

    def list_sensor_configs(self) -> list:
        """Возвращает список имён файлов конфигов (без расширения)."""
        return [p.stem for p in self.configs_dir.glob('*.json')]

    def delete_sensor_config(self, filename: str) -> bool:
        """Удаляет конфиг датчика."""
        filename = self._ensure_json_ext(filename)
        path = self.configs_dir / filename
        if path.exists():
            path.unlink()
            return True
        return False
