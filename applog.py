"""
Единое файловое логирование (Ф-patch2, п.15).

Зачем: многие сбои (автообновление, автопоиск приборов, экспорт XLSX) на реальной
машине проявлялись как «тихо не сработало» — ни трассировки, ни следа. Здесь —
максимально подробный файловый лог, чтобы такие случаи можно было разобрать постфактум,
а не гадать.

Куда: `<кэш>/logs/ivtrace.log` (apppaths.cache_dir() — «папка с кэшем», как просил
заказчик). Ротация по размеру: суммарный потолок ~15 МБ (5 МБ × 3 файла).

Устойчивость: логирование не должно ронять приложение НИКОГДА. Если файл-хендлер не
удаётся создать (нет прав, диск занят, экзотическая среда) — молча остаёмся без файла
(NullHandler), а не падаем в точке, где всего лишь хотели записать строку в журнал.

Использование:
    from applog import setup_logging, get_logger
    setup_logging("gui")          # один раз в точке входа (app.py/run.py/installer.py)
    log = get_logger(__name__)    # в любом модуле
    log.info("...")
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# Корневой логгер приложения. Все модульные логгеры — его потомки
# (get_logger("measurement") -> "ivtrace.measurement"), поэтому один
# хендлер на этом логгере обслуживает весь код, а propagate=False не даёт
# сообщениям всплыть в root и продублироваться там, где кто-то (pytest,
# сторонняя библиотека) уже настроил root-хендлер.
_ROOT_NAME = "ivtrace"

_MAX_BYTES = 5 * 1024 * 1024   # 5 МБ на файл
_BACKUP_COUNT = 2              # + 2 ротации => ~15 МБ суммарно (потолок из ТЗ)

_configured = False
_log_path: Optional[Path] = None


def _resolve_log_path() -> Optional[Path]:
    """
    <кэш>/logs/ivtrace.log. Любой сбой разрешения/создания пути — не ошибка:
    возвращаем None, вызывающий останется без файлового хендлера, но не упадёт.
    Импорт apppaths внутри функции — apppaths ни от чего в applog не зависит,
    но так исключаем любой риск кругового импорта на старте.
    """
    try:
        from apppaths import cache_dir
        log_dir = cache_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "ivtrace.log"
    except Exception:
        return None


def setup_logging(component: str = "app", *, force: bool = False) -> Optional[Path]:
    """
    Идемпотентно настраивает файловое логирование. Повторные вызовы (например
    из разных точек входа в одном процессе) ничего не пересоздают — только
    первый реально ставит хендлер. `component` пишется первой строкой сессии,
    чтобы в общем файле было видно, кто именно писал (gui/cli/installer).

    Возвращает путь к лог-файлу (или None, если файловый лог поднять не вышло).
    force=True — для тестов: сбросить и настроить заново на текущий cache_dir.
    """
    global _configured, _log_path
    logger = logging.getLogger(_ROOT_NAME)

    if force:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        _configured = False

    if _configured:
        return _log_path

    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    path = _resolve_log_path()
    if path is not None:
        try:
            handler = RotatingFileHandler(
                path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            ))
            logger.addHandler(handler)
            _log_path = path
        except Exception:
            path = None

    if path is None:
        # Не смогли поднять файл — NullHandler, чтобы logging не жаловался на
        # «No handlers could be found» и чтобы вызовы log.*() были безвредны.
        logger.addHandler(logging.NullHandler())
        _log_path = None

    _configured = True
    logger.info("=== Логирование запущено (%s), Python %s ===",
                component, sys.version.split()[0])
    return _log_path


def get_logger(name: str) -> logging.Logger:
    """
    Логгер-потомок ивтрейсового корня. `name` обычно __name__ модуля; ведущий
    "ivtrace." не дублируем, а служебные префиксы (__main__) нормализуем в имя
    приложения, чтобы записи не выглядели как «__main__».
    """
    if not name or name == "__main__":
        leaf = "app"
    else:
        leaf = name.rsplit(".", 1)[-1]
    return logging.getLogger(f"{_ROOT_NAME}.{leaf}")


def log_path() -> Optional[Path]:
    """Путь к текущему лог-файлу (или None) — для показа оператору в UI/логе."""
    return _log_path
