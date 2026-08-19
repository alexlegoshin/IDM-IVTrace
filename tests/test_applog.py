"""Тесты файлового логирования (applog, п.15)."""
import logging

import apppaths
import applog


def _isolate_cache(monkeypatch, tmp_path):
    """Логи должны писаться в tmp, а не в реальный кэш пользователя/репозиторий."""
    monkeypatch.setattr(apppaths, "cache_dir", lambda: tmp_path / "Cache")


def test_setup_creates_log_file_in_cache(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    path = applog.setup_logging("test", force=True)
    assert path is not None
    assert path == tmp_path / "Cache" / "logs" / "ivtrace.log"
    assert path.exists()
    applog.get_logger("probe").info("hello-marker")
    for h in logging.getLogger("ivtrace").handlers:
        h.flush()
    assert "hello-marker" in path.read_text(encoding="utf-8")


def test_setup_is_idempotent(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    applog.setup_logging("test", force=True)
    handlers_after_first = list(logging.getLogger("ivtrace").handlers)
    applog.setup_logging("test")  # без force — не должен добавлять хендлеры
    assert logging.getLogger("ivtrace").handlers == handlers_after_first


def test_rotation_config(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    applog.setup_logging("test", force=True)
    from logging.handlers import RotatingFileHandler
    rotators = [h for h in logging.getLogger("ivtrace").handlers
                if isinstance(h, RotatingFileHandler)]
    assert rotators, "ожидался RotatingFileHandler"
    h = rotators[0]
    # суммарный потолок ~15 МБ: 5 МБ * (1 + backupCount 2)
    assert h.maxBytes == 5 * 1024 * 1024
    assert h.backupCount == 2


def test_setup_survives_unwritable_cache(monkeypatch, tmp_path):
    """Если файловый лог поднять нельзя — не падаем, остаёмся без файла."""
    def boom():
        raise OSError("no cache for you")
    monkeypatch.setattr(apppaths, "cache_dir", boom)
    path = applog.setup_logging("test", force=True)
    assert path is None
    # логирование всё равно безвредно
    applog.get_logger("probe").warning("still-fine")


def test_get_logger_normalizes_names():
    assert applog.get_logger("__main__").name == "ivtrace.app"
    assert applog.get_logger("measurement").name == "ivtrace.measurement"
    assert applog.get_logger("pkg.sub.mod").name == "ivtrace.mod"
