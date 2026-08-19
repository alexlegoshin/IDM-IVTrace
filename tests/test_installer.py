"""
installer.py — Tk/сеть/подпроцессы, реально работает только на Windows (см.
план этой сессии); здесь проверяем то, что можно без живого Tk-мейнлупа и
без настоящих диалогов: чистую оркестрацию внутри Installer (копирование,
запись install-записи, поиск даты релиза, запуск exe) с замоканными
installer_core/apppaths — и argparse. Полноценная сквозная проверка — на
Windows-раннере CI и вручную на реальной машине.
"""
import queue

import pytest

import installer


def _bare_installer():
    """Экземпляр Installer без __init__ (без Tk-окна, без фонового потока) — только очередь событий."""
    inst = object.__new__(installer.Installer)
    inst.events = queue.Queue()
    inst.auto_update = False
    return inst


def _drain_statuses(inst):
    statuses = []
    try:
        while True:
            kind, payload, done, box = inst.events.get_nowait()
            if kind == "status":
                statuses.append(payload[0])
            elif done is not None:
                pytest.fail(f"неожиданный блокирующий запрос в тесте: {kind}")
    except queue.Empty:
        pass
    return statuses


def test_main_parses_auto_update_flag():
    parser_calls = []
    import argparse as real_argparse

    class _Args:
        auto_update = True

    orig_parse_args = real_argparse.ArgumentParser.parse_args

    def fake_parse_args(self, argv=None):
        parser_calls.append(argv)
        return _Args()

    import unittest.mock as mock
    with mock.patch.object(real_argparse.ArgumentParser, "parse_args", fake_parse_args):
        with mock.patch("installer.tk.Tk") as fake_tk, mock.patch("installer.Installer") as fake_installer:
            fake_root = fake_tk.return_value
            installer.main(["--auto-update"])
            fake_installer.assert_called_once_with(fake_root, auto_update=True)
            fake_root.mainloop.assert_called_once()
    assert parser_calls == [["--auto-update"]]


def test_payload_dir_is_sibling_ivtrace_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "app_root", lambda: tmp_path)
    inst = _bare_installer()
    assert inst._payload_dir() == tmp_path / "IVTrace"


def test_install_into_happy_path_writes_record_and_shortcuts(monkeypatch, tmp_path):
    inst = _bare_installer()
    monkeypatch.setattr(inst, "_payload_dir", lambda: tmp_path / "payload")

    copy_calls = []
    monkeypatch.setattr(installer.core, "copy_payload", lambda src, dst: copy_calls.append((src, dst)))
    monkeypatch.setattr(installer.core, "own_build_tag", lambda: "v2.0")

    monkeypatch.setattr(installer.core, "get_desktop_path", lambda: tmp_path / "RealDesktop")

    shortcut_calls = []
    monkeypatch.setattr(installer.core, "create_shortcut", lambda exe, path: shortcut_calls.append((exe, path)))

    written = {}
    monkeypatch.setattr(installer, "write_install_record",
                        lambda root, tag, release_date=None: written.update(
                            root=root, tag=tag, release_date=release_date))

    target = tmp_path / "install"
    inst._install_into(target, lookup_release_date=False)

    assert copy_calls == [(tmp_path / "payload", target)]
    assert written == {"root": target, "tag": "v2.0", "release_date": None}
    assert len(shortcut_calls) == 2  # Десктоп + Меню Пуск
    assert all(exe == target / "IVTrace.exe" for exe, _ in shortcut_calls)
    assert shortcut_calls[0][1] == tmp_path / "RealDesktop" / "IVTrace.lnk"  # реальный (возможно, переопределённый) Desktop, не Path.home()/"Desktop"

    statuses = _drain_statuses(inst)
    assert "Копирование файлов…" in statuses
    assert "Создание ярлыков…" in statuses


def test_install_into_falls_back_to_home_desktop_when_get_desktop_path_fails(monkeypatch, tmp_path):
    inst = _bare_installer()
    monkeypatch.setattr(inst, "_payload_dir", lambda: tmp_path / "payload")
    monkeypatch.setattr(installer.core, "copy_payload", lambda src, dst: None)
    monkeypatch.setattr(installer.core, "own_build_tag", lambda: "v2.0")
    monkeypatch.setattr(installer, "write_install_record", lambda *a, **kw: None)

    def failing_get_desktop_path():
        raise RuntimeError("powershell недоступен")
    monkeypatch.setattr(installer.core, "get_desktop_path", failing_get_desktop_path)

    shortcut_calls = []
    monkeypatch.setattr(installer.core, "create_shortcut", lambda exe, path: shortcut_calls.append((exe, path)))

    inst._install_into(tmp_path / "install", lookup_release_date=False)

    assert shortcut_calls[0][1] == installer.Path.home() / "Desktop" / "IVTrace.lnk"


def test_install_into_still_creates_start_menu_shortcut_when_desktop_shortcut_fails(monkeypatch, tmp_path):
    """Ярлык на рабочем столе и ярлык в Пуск — независимые попытки: падение
    одной не должно молча гасить другую (раньше обе создавались в одном
    try/except, и первая же ошибка обрывала обе)."""
    inst = _bare_installer()
    monkeypatch.setattr(inst, "_payload_dir", lambda: tmp_path / "payload")
    monkeypatch.setattr(installer.core, "copy_payload", lambda src, dst: None)
    monkeypatch.setattr(installer.core, "own_build_tag", lambda: "v2.0")
    monkeypatch.setattr(installer.core, "get_desktop_path", lambda: tmp_path / "RealDesktop")
    monkeypatch.setattr(installer, "write_install_record", lambda *a, **kw: None)

    shortcut_calls = []

    def create_shortcut(exe, path):
        if "RealDesktop" in str(path):
            raise OSError("доступ запрещён")
        shortcut_calls.append((exe, path))
    monkeypatch.setattr(installer.core, "create_shortcut", create_shortcut)

    inst._install_into(tmp_path / "install", lookup_release_date=False)

    assert len(shortcut_calls) == 1
    assert "Start Menu" in str(shortcut_calls[0][1])


def test_install_into_lookup_release_date_when_requested(monkeypatch, tmp_path):
    inst = _bare_installer()
    monkeypatch.setattr(inst, "_payload_dir", lambda: tmp_path / "payload")
    monkeypatch.setattr(installer.core, "copy_payload", lambda src, dst: None)
    monkeypatch.setattr(installer.core, "own_build_tag", lambda: "v2.0")
    monkeypatch.setattr(installer.core, "get_desktop_path", lambda: tmp_path / "RealDesktop")
    monkeypatch.setattr(installer.core, "create_shortcut", lambda exe, path: None)
    monkeypatch.setattr(installer.core, "fetch_releases",
                        lambda: [{"tag_name": "v2.0", "published_at": "2026-08-10T00:00:00Z"}])

    written = {}
    monkeypatch.setattr(installer, "write_install_record",
                        lambda root, tag, release_date=None: written.update(release_date=release_date))

    inst._install_into(tmp_path / "install", lookup_release_date=True)
    assert written["release_date"] == "2026-08-10T00:00:00Z"


def test_install_into_shows_error_and_stops_on_copy_failure(monkeypatch, tmp_path):
    inst = _bare_installer()
    monkeypatch.setattr(inst, "_payload_dir", lambda: tmp_path / "payload")
    monkeypatch.setattr(installer.core, "copy_payload",
                        lambda src, dst: (_ for _ in ()).throw(OSError("locked")))
    write_calls = []
    monkeypatch.setattr(installer, "write_install_record", lambda *a, **kw: write_calls.append((a, kw)))

    errors = []
    monkeypatch.setattr(inst, "_show_error", lambda title, text: errors.append((title, text)))

    result = inst._install_into(tmp_path / "install", lookup_release_date=False)

    assert result is False  # сигнал вызывающему: установка не состоялась
    assert len(errors) == 1
    assert "закройте" in errors[0][1].lower()
    assert write_calls == []  # не пишем запись об установке, которая не удалась


def test_do_auto_update_launches_app_after_successful_install(monkeypatch, tmp_path):
    """
    Баг-репорт (главный симптом «обновление не срабатывает»): после
    доустановки СТАРЫЙ код закрывался, ничего не запуская. Теперь
    _do_auto_update при успехе ДОЛЖЕН запустить приложение.
    """
    inst = _bare_installer()
    inst.auto_update = True
    target = tmp_path / "install"
    monkeypatch.setattr(installer, "read_install_record",
                        lambda: {"install_root": str(target), "tag": "v2.0"})
    install_calls = []
    monkeypatch.setattr(inst, "_install_into",
                        lambda t, lookup_release_date: install_calls.append(t) or True)
    launched = []
    monkeypatch.setattr(inst, "_launch", lambda t: launched.append(t))

    inst._do_auto_update()

    assert install_calls == [target]
    assert launched == [target]  # приложение запущено после установки


def test_do_auto_update_does_not_launch_when_install_fails(monkeypatch, tmp_path):
    """Если доустановка не удалась — приложение НЕ запускаем (не выдаём сбой за успех)."""
    inst = _bare_installer()
    inst.auto_update = True
    target = tmp_path / "install"
    monkeypatch.setattr(installer, "read_install_record",
                        lambda: {"install_root": str(target), "tag": "v2.0"})
    monkeypatch.setattr(inst, "_install_into", lambda t, lookup_release_date: False)
    launched = []
    monkeypatch.setattr(inst, "_launch", lambda t: launched.append(t))

    inst._do_auto_update()

    assert launched == []


def test_lookup_own_release_date_finds_matching_tag(monkeypatch):
    inst = _bare_installer()
    monkeypatch.setattr(installer.core, "fetch_releases", lambda: [
        {"tag_name": "v1.9", "published_at": "2026-07-01T00:00:00Z"},
        {"tag_name": "v2.0", "published_at": "2026-08-10T00:00:00Z"},
    ])
    assert inst._lookup_own_release_date("v2.0") == "2026-08-10T00:00:00Z"


def test_lookup_own_release_date_silent_on_network_failure(monkeypatch):
    inst = _bare_installer()
    monkeypatch.setattr(installer.core, "fetch_releases",
                        lambda: (_ for _ in ()).throw(OSError("no internet")))
    assert inst._lookup_own_release_date("v2.0") is None


def test_launch_spawns_exe_when_present(monkeypatch, tmp_path):
    inst = _bare_installer()
    target = tmp_path / "install"
    target.mkdir()
    exe = target / "IVTrace.exe"
    exe.write_bytes(b"x")

    calls = []
    monkeypatch.setattr(installer.subprocess, "Popen", lambda args, cwd=None: calls.append((args, cwd)))
    inst._launch(target)
    assert calls == [([str(exe)], str(target))]


def test_launch_does_nothing_when_exe_missing(monkeypatch, tmp_path):
    inst = _bare_installer()
    calls = []
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    inst._launch(tmp_path / "nowhere")
    assert calls == []


def test_download_and_relaunch_update_falls_back_silently_without_windows_asset(monkeypatch, tmp_path):
    inst = _bare_installer()
    launched = []
    monkeypatch.setattr(inst, "_launch", lambda target: launched.append(target))
    monkeypatch.setattr(installer.core, "pick_windows_asset", lambda release: None)

    inst._download_and_relaunch_update({"assets": []}, tmp_path / "install")
    assert launched == [tmp_path / "install"]
