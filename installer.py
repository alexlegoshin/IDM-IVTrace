"""
Setup.exe — единый инсталлятор/апдейтер IVTrace (Ф6), только GUI (в CLI не
встроен, отдельная точка входа от run.py/app.py).

Логика решений — installer_core.py (чистые функции, покрыты тестами);
здесь только Tk-сплэш (баннер Legoshi + строка статуса) и раскладка по
состояниям, описанная в PLAN_V2.md, Ф6:

    нет валидной install-записи  -> спросить папку, установить с нуля
    есть запись, есть интернет   -> сверить с GitHub Releases (см.
                                     installer_core.select_latest_release):
                                       новее      -> предложить обновиться
                                       не новее   -> предложить переустановить
                                     "нет"/"отмена" в обоих случаях, как и
                                     любая сетевая ошибка при проверке, —
                                     просто запускаем уже установленную
                                     версию, без единого диалога об ошибке
    --auto-update                -> без диалогов: доустановить в уже
                                     известный install_root (так запускает
                                     СЕБЯ новая версия, скачанная и
                                     распакованная СТАРЫМ Setup.exe после
                                     согласия пользователя на обновление)

Устройство UI: фоновый поток (_worker) делает всю работу и общается с Tk-
потоком через очередь событий — тот же приём, что и в gui.py (там же
_drain_events). Отличие: здесь нужны ещё и СИНХРОННЫЕ вопросы (да/нет,
выбор папки) — Tk обязан показывать диалоги из своего потока, поэтому
рабочий поток кладёт в очередь запрос + threading.Event и блокируется на
нём, а Tk-поток отвечает и снимает блокировку (см. _ask).
"""
import argparse
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import messagebox, filedialog
from typing import Optional

import installer_core as core
from apppaths import assets_dir, app_root, read_install_record, write_install_record

BG = "#0d1117"  # тон баннера — без белой рамки по краям окна
FG = "#e6e6e6"


def _ask(events: "queue.Queue", kind: str, *payload):
    """Кладёт запрос в очередь к Tk-потоку и блокируется на ответ (см. модульный докстринг)."""
    done = threading.Event()
    box = {}
    events.put((kind, payload, done, box))
    done.wait()
    return box.get("result")


class Installer:
    def __init__(self, root: tk.Tk, auto_update: bool):
        self.root = root
        self.auto_update = auto_update
        self.events = queue.Queue()
        self._build_ui()
        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(100, self._drain)

    # --------------------------------------------------------------- UI
    def _build_ui(self):
        self.root.overrideredirect(True)
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)

        banner_path = assets_dir() / "legoshi_banner.png"
        img = tk.PhotoImage(file=str(banner_path)) if banner_path.exists() else None
        self._banner_img = img  # ссылка на PhotoImage — иначе Tk соберёт её мусором

        w = img.width() if img else 700
        h = img.height() if img else 300
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x, y = (screen_w - w) // 2, (screen_h - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        if img:
            tk.Label(self.root, image=img, bg=BG, bd=0).place(x=0, y=0, width=w, height=h)

        self.status_var = tk.StringVar(value="")
        tk.Label(self.root, textvariable=self.status_var, bg=BG, fg=FG,
                 font=("Segoe UI", 9)).place(relx=0.5, y=h - 18, anchor="s")

    # ----------------------------------------------------------- Tk-поток
    def _drain(self):
        try:
            while True:
                kind, payload, done, box = self.events.get_nowait()
                self._handle(kind, payload, done, box)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._drain)

    def _handle(self, kind, payload, done, box):
        if kind == "status":
            self.status_var.set(payload[0])
        elif kind == "ask_yes_no":
            title, text = payload
            box["result"] = messagebox.askyesno(title, text, parent=self.root)
            done.set()
        elif kind == "ask_directory":
            (initial,) = payload
            box["result"] = filedialog.askdirectory(initialdir=initial, parent=self.root)
            done.set()
        elif kind == "show_error":
            title, text = payload
            messagebox.showerror(title, text, parent=self.root)
            done.set()
        elif kind == "finish":
            self.root.destroy()

    # -------------------------------------------------------------- worker
    def _status(self, text: str):
        self.events.put(("status", (text,), None, None))

    def _ask_yes_no(self, title: str, text: str) -> bool:
        return bool(_ask(self.events, "ask_yes_no", title, text))

    def _ask_directory(self, initial: str) -> str:
        return _ask(self.events, "ask_directory", initial) or ""

    def _show_error(self, title: str, text: str):
        _ask(self.events, "show_error", title, text)

    def _worker(self):
        try:
            if self.auto_update:
                self._do_auto_update()
            else:
                self._do_interactive()
        except Exception as e:
            # Ожидаемые сбои (сеть при проверке обновлений, блокировка
            # файлов при копировании) перехвачены по месту и тихие/с
            # понятным сообщением — сюда долетает только то, что мы не
            # предвидели, и молчать об этом было бы хуже, чем показать.
            self._show_error("Ошибка установки", str(e))
        finally:
            self.events.put(("finish", (), None, None))

    # ------------------------------------------------------------ сценарии
    def _payload_dir(self) -> Path:
        """Папка IVTrace/ рядом с самим Setup.exe (см. apppaths.app_root())."""
        return app_root() / "IVTrace"

    def _do_auto_update(self):
        self._status("Обновление…")
        record = read_install_record()
        if not record or not record.get("install_root"):
            self._show_error("Обновление", "Не найдена информация о текущей установке.")
            return
        self._install_into(Path(record["install_root"]), lookup_release_date=True)
        self._status("Готово")

    def _do_interactive(self):
        record = read_install_record()
        target = Path(record["install_root"]) if record and record.get("install_root") else None
        if target is None or not target.is_dir():
            self._fresh_install()
            return

        self._status("Проверка обновлений…")
        try:
            releases = core.fetch_releases()
            latest = core.select_latest_release(releases)
        except Exception:
            # Нет интернета/GitHub недоступен — тихо, без единого
            # уведомления (явное требование): просто запускаем то, что уже
            # установлено.
            self._launch(target)
            return

        if latest is not None and core.is_newer(
            latest.get("tag_name", ""), latest.get("published_at"),
            record.get("tag", ""), record.get("release_date"),
        ):
            self._status("Обновление доступно")
            yes = self._ask_yes_no(
                "Обновление IVTrace",
                f"Доступна новая версия {latest.get('tag_name')} "
                f"(сейчас установлена {record.get('tag', '?')}). Установить?",
            )
            if yes:
                self._download_and_relaunch_update(latest, target)
            else:
                self._launch(target)
            return

        self._offer_reinstall(target, record)

    def _offer_reinstall(self, target: Path, record: dict):
        yes = self._ask_yes_no(
            "IVTrace уже установлена",
            f"Уже установлена версия {record.get('tag', '?')} в {target}.\nПереустановить?",
        )
        if yes:
            self._install_into(target, lookup_release_date=True)
            self._status("Готово")
        else:
            self._launch(target)

    def _fresh_install(self):
        self._status("Выбор папки установки…")
        base = self._ask_directory(str(Path.home()))
        if not base:
            self._status("Установка отменена")
            return
        target = Path(base) / "Legoshi" / "IVTrace"
        # Полностью офлайн: без обращения к GitHub, дата релиза неизвестна.
        self._install_into(target, lookup_release_date=False)
        self._status("Готово")

    def _install_into(self, target: Path, lookup_release_date: bool):
        self._status("Копирование файлов…")
        try:
            core.copy_payload(self._payload_dir(), target)
        except OSError as e:
            self._show_error(
                "Не удалось скопировать файлы",
                f"Закройте IVTrace, если она запущена, и запустите установку снова.\n\n{e}",
            )
            return

        tag = core.own_build_tag() or "dev"
        release_date = self._lookup_own_release_date(tag) if lookup_release_date else None
        write_install_record(target, tag=tag, release_date=release_date)

        self._status("Создание ярлыков…")
        exe = target / "IVTrace.exe"
        try:
            core.create_shortcut(exe, Path.home() / "Desktop" / "IVTrace.lnk")
            start_menu = (Path.home() / "AppData" / "Roaming" / "Microsoft" /
                          "Windows" / "Start Menu" / "Programs")
            core.create_shortcut(exe, start_menu / "IVTrace.lnk")
        except Exception:
            pass  # ярлык — удобство, не критично для самой установки

    def _lookup_own_release_date(self, tag: str) -> Optional[str]:
        """
        Лучшее из возможного: если сеть есть (мы уже ей воспользовались,
        чтобы попасть в эту ветку), уточняем дату публикации СВОЕГО тега —
        она не зашита в билд (на момент сборки релиз ещё не опубликован,
        см. план), поэтому подтягиваем отдельно. Любая ошибка — молча
        оставляем release_date неизвестным, апдейтер в следующий раз просто
        сравнит по тегу.
        """
        try:
            for release in core.fetch_releases():
                if release.get("tag_name") == tag:
                    return release.get("published_at")
        except Exception:
            pass
        return None

    def _download_and_relaunch_update(self, release: dict, target: Path):
        asset = core.pick_windows_asset(release)
        if asset is None:
            self._launch(target)
            return
        self._status("Скачивание обновления…")
        tmp_dir = Path(tempfile.mkdtemp(prefix="ivtrace_update_"))
        zip_path = tmp_dir / asset["name"]
        try:
            urllib.request.urlretrieve(asset["browser_download_url"], zip_path)
            self._status("Распаковка…")
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_dir)
        except Exception:
            # Скачивание/распаковка сорвались — тот же тихий фолбэк, что и
            # при недоступной проверке обновлений.
            self._launch(target)
            return

        new_setup = tmp_dir / "Setup.exe"
        if not new_setup.exists():
            self._launch(target)
            return

        # Popen, не wait(): сами закрываемся немедленно (см. finally в
        # _worker), не держим ничего занятым для новой установки.
        subprocess.Popen([str(new_setup), "--auto-update"])

    def _launch(self, target: Path):
        exe = target / "IVTrace.exe"
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(target))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Установка/обновление IVTrace.")
    parser.add_argument(
        "--auto-update", action="store_true",
        help="Без диалогов: доустановить в уже известную install_root "
             "(так запускает себя новая версия после согласия пользователя на обновление).",
    )
    args = parser.parse_args(argv)

    root = tk.Tk()
    Installer(root, auto_update=args.auto_update)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
