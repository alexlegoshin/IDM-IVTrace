import json
from pathlib import Path


class ConfigManager:
    """
    Отвечает за чтение/запись JSON-конфига с параметрами последнего измерения
    (excitation_type, X_start, X_stop, X_step, V_limit, delay, cooling_delay,
    label). Направление (forward/reverse) не хранится — обе ветви снимаются
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
