import json
from pathlib import Path
from typing import Optional

class ConfigManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

# class ConfigManager:
#     def __init__(self, config_path: Path = Path("C:/IVTraceData/ivtrace_config.json")):
#         self.config_path = config_path
#         self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict:
        if self.config_path.exists():
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

    def save(self, config: dict):
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)