"""
Графический интерфейс IVTrace (классический Tkinter + ttk).

Минималистичный, светлый, без лишних зависимостей — Tkinter поставляется
вместе с Python и легко собирается в portable exe. Интерфейс переиспользует
то же ядро, что и CLI (orchestrate.run_measurement_session, analysis,
visa_backend, selftest), поэтому логика измерения полностью идентична.

Устройство:
  - при старте в фоне выполняется предполётная проверка (NI-VISA + самотесты);
    кнопка «Старт» разблокируется только если проверка пройдена — это
    защита оборудования от запуска на сломанном коде/без VISA;
  - измерение идёт в отдельном потоке, весь вывод ядра (print) перехватывается
    в журнал; кнопка «Стоп» кооперативно прерывает проход между точками;
  - вкладка «Данные» строит тот же график, что и CLI analyze; сам холст
    отображается на отдельной вкладке «График» (переключение автоматическое).
"""
import io
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog
from tkinter import font as tkfont

import pandas as pd

from apppaths import (
    default_data_dir, sensor_config_dir, work_dir, set_work_dir, cache_dir, config_dir,
    multimeter_cfg_dir, voltmeter_cfg_dir, current_source_cfg_dir, voltage_source_cfg_dir,
    clear_results_cache, assets_dir,
)
from calibration import known_models, list_calibration_rows, set_calibration_record, delete_calibration_record
from config import ConfigManager, SensorConfigManager
from cli import current_sweep_max_abs, make_csv_filename, validate_measure_params
from discovery import DiscoveryService
from instruments import identify_instrument
from limits import (
    relay_current_warning, relay_current_block_reason,
    smooth_ramp_warning, SMOOTH_RAMP_WARN_CURRENT_A,
)
from measurement import (
    EXCITATION_UNITS,
    DEFAULT_AVERAGING_COUNT, DEFAULT_AVERAGING_DELAY, DEFAULT_DISCARD_FIRST,
    DEFAULT_ADAPTIVE_COOLING_MIN_DELAY, DEFAULT_ADAPTIVE_COOLING_MAX_DELAY,
    MAX_MEASUREMENT_ATTEMPTS,
    estimate_duration_seconds,
)
from sweep import Branch, DirectionPreset, plan_sweep, plan_custom_sweep, preset_applies
from applog import get_logger

_log = get_logger(__name__)


# п.33 — минимализм: бело-кремовый фон, чёрный текст, геометричный
# гротеск (Century Gothic — не всегда есть на голой Windows без Office;
# берём первый реально установленный шрифт из списка, см. _pick_font).
BG = "#faf7f0"
CARD = "#fffdf8"
CARD_BORDER = "#e6e1d3"
TEXT = "#1c1c19"
MUTED = "#7a7566"
ACCENT = "#1c1c19"          # кнопки основного действия — почти чёрные, не цветной акцент
ACCENT_ACTIVE = "#3a3a34"
DANGER = "#b3261e"
DANGER_ACTIVE = "#8f1e18"
OK_COLOR = "#3f6b3f"
ERR_COLOR = "#b3261e"
BUSY_COLOR = "#9c7a29"

_FONT_CANDIDATES = ("Century Gothic", "Yanone Kaffeesatz", "Gill Sans MT", "Trebuchet MS", "Segoe UI")
_FONT_FAMILY = None


def _pick_font(root) -> str:
    """Первый реально установленный шрифт из _FONT_CANDIDATES; Segoe UI — гарантированный запасной на Windows."""
    global _FONT_FAMILY
    if _FONT_FAMILY is None:
        available = set(tkfont.families(root))
        _FONT_FAMILY = next((f for f in _FONT_CANDIDATES if f in available), "Segoe UI")
    return _FONT_FAMILY


_wheel_dispatch_installed = False


def _install_global_wheel_dispatch(any_widget):
    """
    Единственная на всё приложение привязка колеса мыши (bind_all), общая
    для всех _ScrollableFrame. Раньше каждый экземпляр вешал/снимал
    bind_all по своим <Enter>/<Leave> на canvas — но виджеты внутри body
    (Entry, Combobox, Label...) в Tk являются отдельными дочерними окнами
    поверх canvas, и курсор, попадая на них, генерирует Leave у canvas
    (колесо переставало работать почти везде, кроме голых промежутков —
    отсюда «колесико не всегда срабатывало»). Вместо слежения за границами
    canvas разбираем событие по фактическому виджету под курсором и идём
    вверх по .master, пока не найдём владельца — так работает независимо
    от того, над каким конкретно дочерним виджетом сейчас курсор.
    """
    global _wheel_dispatch_installed
    if _wheel_dispatch_installed:
        return
    _wheel_dispatch_installed = True
    root = any_widget.winfo_toplevel()

    def _dispatch(event):
        try:
            under = event.widget.winfo_containing(event.x_root, event.y_root)
        except Exception:
            under = event.widget
        w = under
        while w is not None:
            owner = getattr(w, "_ivtrace_scroll_owner", None)
            if owner is not None:
                owner._on_mousewheel(event)
                return
            w = w.master if hasattr(w, "master") else None

    root.bind_all("<MouseWheel>", _dispatch)


class _ScrollableFrame(ttk.Frame):
    """
    Вертикальный скролл без лишней навороченности (п.33): Canvas + Frame
    внутри, колесо мыши, тонкий скроллбар без явной рамки. Нужен потому,
    что при табличной раскладке левая панель параметров всё равно иногда
    выше, чем помещается на маленьком мониторе — без скролла контент
    просто обрезался бы снизу вместо того, чтобы прокручиваться.

    Использование: строить содержимое внутри self.body (это ttk.Frame),
    а не внутри самого _ScrollableFrame.
    """

    def __init__(self, parent, bg=BG):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(self, highlightthickness=0, bg=bg, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.body = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # Владелец для глобального диспетчера колеса — виден с любого
        # дочернего виджета через цепочку .master (см. _install_global_wheel_dispatch).
        self.canvas._ivtrace_scroll_owner = self
        _install_global_wheel_dispatch(self.canvas)

    def _on_body_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_mousewheel(self, event):
        # Если весь контент и так помещается в видимую область — скроллить
        # нечего; без этой проверки yview_scroll всё равно немного «дёргал»
        # canvas на месте, хотя прокручивать было некуда (баг: «поле
        # двигается, даже если на нём всё видно»).
        if self.body.winfo_height() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class _QueueWriter(io.TextIOBase):
    """Файлоподобный объект: перенаправляет stdout/stderr ядра в очередь событий GUI."""

    def __init__(self, events: queue.Queue):
        self.events = events

    def write(self, s):
        if s:
            self.events.put(("log", s))
        return len(s)

    def flush(self):
        pass


class _SessionSlot:
    """
    Ссылка на приборы идущего измерения, разделяемая двумя потоками.

    Заполняет её рабочий поток (когда orchestrate откроет приборы), читает
    UI-поток (когда оператор жмёт «Стоп» или закрывает окно). Замок нужен не
    столько от гонки за саму ссылку — присваивание атрибута в CPython
    атомарно, — сколько чтобы «Стоп», нажатый дважды подряд, не запустил две
    аварийные последовательности внахлёст.
    """

    def __init__(self):
        self._handle = None
        self._lock = threading.Lock()

    def set(self, handle):
        with self._lock:
            self._handle = handle

    def get(self):
        with self._lock:
            return self._handle

    def clear(self):
        with self._lock:
            self._handle = None


class IVTraceGUI:
    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        # Перехват ЛЮБОГО необработанного исключения в Tk-колбэке (обработчики
        # кнопок, root.after и т.п.). Раньше Tk просто печатал трассировку в
        # stderr (в собранном exe невидимую) и молча продолжал — так «вылеты»
        # разных функций оставались без следа. Теперь пишем в файл-лог с полной
        # трассировкой и показываем оператору короткое сообщение.
        self.root.report_callback_exception = self._on_tk_callback_exception
        # work_dir() уважает переопределение из UI (п.23, apppaths.set_work_dir);
        # явный --data-dir из командной строки всё равно приоритетнее.
        self.data_dir = Path(getattr(args, "data_dir", None) or work_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_mgr = ConfigManager(self.data_dir / "ivtrace_config.json")
        # Конфигурационная папка, НЕ рабочая (self.data_dir) — см. п.39:
        # рабочую папку оператор может перенастроить и засорить CSV,
        # профили датчиков от этого не должны зависеть.
        self.sensor_config_mgr = SensorConfigManager(sensor_config_dir())

        self.events = queue.Queue()
        self.stop_event = threading.Event()
        # Ручка на приборы идущего измерения — через неё «Стоп» обесточивает
        # стенд, не дожидаясь, пока цикл дойдёт до проверки stop_event.
        self._session = _SessionSlot()
        self.worker = None
        self._preflight_ok = False
        # "No Relay" (см. _apply_discovery_state): пока плата реле не
        # найдена, полярность заблокирована на Branch.NO_RELAY — физически
        # без реле нельзя ни коммутировать, ни выбрать positive/negative/both.
        self._branch_locked_no_relay = False
        self.last_csv = None
        self._current_fig = None
        # Вкладка «График» (Ф3): какой файл сейчас разобран (может быть не
        # last_csv — оператор мог открыть произвольный старый CSV, п.20),
        # его текущий DataFrame (для правки точек, п.26) и параметры
        # последнего измерения (для автопостроения по его окончании, п.22).
        self.plot_csv_path = None
        self._current_df = None
        self._last_measure_params = None

        self.skip_selftest_var = tk.BooleanVar(value=bool(getattr(args, "skip_selftest", False)))
        self.excitation_var = tk.StringVar(value="current")
        self.output_var = tk.StringVar(value="current")
        self.suppress_warnings_var = tk.BooleanVar(value=False)

        # Все переменные формы созданы здесь заранее (не по ходу постройки
        # виджетов) — вкладки строятся отдельными методами в произвольном
        # порядке, а предпросмотр развёртки (см. _update_sweep_preview)
        # должен уметь читать branch/preset независимо от того, какая
        # вкладка успела построиться первой.
        self.stop_on_error_var = tk.BooleanVar(value=False)
        self.branch_var = tk.StringVar(value=Branch.BOTH.value)
        self.preset_var = tk.StringVar(value=DirectionPreset.DIVERGING.value)
        self.discard_first_var = tk.BooleanVar(value=DEFAULT_DISCARD_FIRST)
        self.adaptive_cooling_var = tk.BooleanVar(value=False)
        self.smooth_ramp_var = tk.BooleanVar(value=False)
        self.custom_program_var = tk.BooleanVar(value=False)
        self.show_labels_var = tk.BooleanVar(value=False)
        self.auto_range_var = tk.BooleanVar(value=True)
        # Возврат приборов в Local после цикла (п.8) — по умолчанию включён.
        self.restore_local_var = tk.BooleanVar(value=True)
        # Плавный проход нуля для FULL_CYCLE (п.18).
        self.zero_crossing_smooth_var = tk.BooleanVar(value=False)

        # Ручной режим вне измерительного цикла (Ф4, п.13/40) — открытая
        # сессия живёт между кликами (реле/уставка), не одна операция за
        # вызов, поэтому хранится отдельно от self._session (та — только на
        # время измерения, см. _measure_worker).
        self._manual_session = None
        self._manual_lock = threading.Lock()

        # Обратный отсчёт (п.15, только GUI).
        self._countdown_remaining = None
        self._countdown_after_id = None
        self._countdown_finish_text = ""

        # Баннер предупреждения (п.16) — открывается только вручную.
        self._warning_banner = None

        self._closing = False
        self._after_id = None
        self._discovery_after_id = None

        self._build_style()
        self._build_ui()
        self._prefill_from_config()

        # Служба обнаружения приборов (Ф4, п.25): фоновые периодические
        # сканы, UI просто читает последний снимок — то же устройство
        # взаимодействия поток<->Tk, что и у измерения (события через
        # очередь/после через root.after, а не прямые вызовы в виджеты из
        # чужого потока). НЕ участвует в открытии приборов для самого
        # измерения — тот путь (_resolve_instruments) всегда сканирует
        # заново, см. discovery.py и PLAN_V2.md, п.25.
        self.discovery = DiscoveryService(
            self._make_discovery_rm,
            config_dirs={
                'multimeter': multimeter_cfg_dir(),
                'voltmeter': voltmeter_cfg_dir(),
                'current_source': current_source_cfg_dir(),
                'voltage_source': voltage_source_cfg_dir(),
            },
        )
        self.discovery.start()
        self._discovery_after_id = self.root.after(1000, self._refresh_discovery_ui)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.root.after(120, self._drain_events)
        self._run_preflight()

    @staticmethod
    def _make_discovery_rm():
        from visa_backend import make_resource_manager
        return make_resource_manager()

    def _load_window_icon(self):
        """
        Иконка окна/панели задач — квадратный значок без мелкого текста
        (читается и совсем маленьким, в отличие от лого с надписью TRACE в
        заголовке, см. _build_ui). Отдаём ПОЛНЫЙ спуск размеров, какие есть
        (16 → 256) — ОС (Dock на macOS, панель задач/Alt-Tab на Windows)
        сама берёт подходящий, вместо того чтобы растягивать маленький
        источник и размывать (баг-репорт: иконка в Dock была смазанной,
        потому что на iconphoto отдавались только 16–64). Ссылки на
        PhotoImage держим на self — иначе Tk соберёт их мусором и иконка
        исчезнет.
        """
        self._icon_images = []
        for size in (16, 32, 48, 64, 128, 256, 512):
            path = assets_dir() / f"icon_{size}.png"
            if path.exists():
                self._icon_images.append(tk.PhotoImage(file=str(path)))
        if self._icon_images:
            try:
                self.root.iconphoto(True, *self._icon_images)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------ style
    def _build_style(self):
        self.root.title("IVTrace")
        self._load_window_icon()
        self.root.geometry("1180x760")
        # Табличная раскладка + скролл внутри вкладок (см. _ScrollableFrame)
        # держит панель управляемой даже на небольшом экране — минимальный
        # размер окна снижен по сравнению с прежним нескроллящимся вариантом.
        self.root.minsize(820, 520)
        self.root.configure(bg=BG)
        # п.24: развёрнуто на весь экран при запуске (НЕ fullscreen — заголовок
        # окна и панель задач остаются на месте, это не то же самое, что
        # -fullscreen). "zoomed" — стандартное для Windows состояние Tk-окна.
        try:
            self.root.state("zoomed")
        except tk.TclError:
            pass

        family = _pick_font(self.root)
        self.font_family = family

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base_font = (family, 10)
        style.configure(".", font=base_font, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=(family, 18))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(family, 9))
        style.configure("TLabelframe", background=BG, bordercolor=CARD_BORDER)
        style.configure("TLabelframe.Label", background=BG, foreground=TEXT, font=(family, 10))
        style.configure("TNotebook", background=BG, bordercolor=CARD_BORDER, tabmargins=(2, 4, 2, 0))
        style.configure("TNotebook.Tab", background=BG, foreground=MUTED, padding=(14, 7), font=(family, 10))
        style.map("TNotebook.Tab",
                  background=[("selected", CARD)],
                  foreground=[("selected", TEXT)])
        style.configure("TEntry", padding=4, fieldbackground=CARD, bordercolor=CARD_BORDER)
        style.configure("TCombobox", padding=4, fieldbackground=CARD, bordercolor=CARD_BORDER)
        style.configure("TCheckbutton", background=BG, foreground=TEXT)
        style.configure("TRadiobutton", background=BG, foreground=TEXT)
        style.configure("TButton", padding=(12, 6), font=(family, 10))
        style.configure("Accent.TButton", padding=(16, 8), foreground=CARD,
                        background=ACCENT, font=(family, 10), borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", ACCENT_ACTIVE), ("disabled", "#b8b3a3")])
        style.configure("Danger.TButton", padding=(14, 8), foreground=CARD,
                        background=DANGER, font=(family, 10), borderwidth=0)
        style.map("Danger.TButton",
                  background=[("active", DANGER_ACTIVE), ("disabled", "#c9a6a3")])
        style.configure("Vertical.TScrollbar", background=BG, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED, relief="flat")

    # --------------------------------------------------------------------- ui
    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # ---- header ----
        header = ttk.Frame(self.root, padding=(18, 14, 18, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        # Логотип вместо текстовой надписи "IVTrace" — картинка заранее
        # отрисована под нужный размер (56 px высотой: достаточно, чтобы
        # надпись TRACE внутри неё оставалась читаемой, но не крупнее
        # заголовка), поэтому грузим напрямую tk.PhotoImage без Pillow как
        # runtime-зависимости. Ссылку держим на self — иначе Tk соберёт
        # картинку мусором и заголовок останется пустым.
        logo_path = assets_dir() / "logo_header.png"
        if logo_path.exists():
            self._header_logo_img = tk.PhotoImage(file=str(logo_path))
            ttk.Label(header, image=self._header_logo_img).grid(row=0, column=0, sticky="w")
        else:
            ttk.Label(header, text="IVTrace", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Амплитудная характеристика датчиков тока/напряжения",
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w")

        self.status_dot = tk.Canvas(header, width=12, height=12, bg=BG, highlightthickness=0)
        self.status_dot.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 8))
        self._dot = self.status_dot.create_oval(2, 2, 10, 10, fill=MUTED, outline="")
        self.status_label = ttk.Label(header, text="Инициализация…", style="Muted.TLabel")
        self.status_label.grid(row=0, column=2, rowspan=2, sticky="e")

        # ---- body: two columns (левая — фиксированная ширина под форму,
        # немного растёт с окном; правая — журнал/график, забирает всё
        # остальное место) ----
        body = ttk.Frame(self.root, padding=(18, 6, 18, 6))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, minsize=360, weight=1)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(0, weight=1)

        self._build_params(body)
        self._build_right(body)

        # ---- footer / preflight bar ----
        footer = ttk.Frame(self.root, padding=(18, 6, 18, 12))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.footer_label = ttk.Label(footer, text="", style="Muted.TLabel")
        self.footer_label.grid(row=0, column=0, sticky="w")

        footer_btns = ttk.Frame(footer)
        footer_btns.grid(row=0, column=1, sticky="e")
        ttk.Button(footer_btns, text="Рабочая папка…", command=self._open_work_dir_dialog).pack(side="left", padx=(0, 6))

        # Баг-репорт: "чёрт ногу сломит" среди папок с конфигами — один
        # выпадающий список вместо кучи отдельных кнопок (не перегружать UI).
        open_folder_mb = ttk.Menubutton(footer_btns, text="Открыть папку ▾")
        open_folder_menu = tk.Menu(open_folder_mb, tearoff=False)
        open_folder_menu.add_command(label="Рабочая папка (CSV/графики)",
                                     command=lambda: self._open_folder(work_dir()))
        open_folder_menu.add_command(label="Кэш", command=lambda: self._open_folder(cache_dir()))
        open_folder_menu.add_command(label="Профили датчиков",
                                     command=lambda: self._open_folder(sensor_config_dir()))
        open_folder_menu.add_command(label="Конфиги: мультиметр как амперметр",
                                     command=lambda: self._open_folder(multimeter_cfg_dir()))
        open_folder_menu.add_command(label="Конфиги: мультиметр как вольтметр",
                                     command=lambda: self._open_folder(voltmeter_cfg_dir()))
        open_folder_menu.add_command(label="Конфиги: источники тока",
                                     command=lambda: self._open_folder(current_source_cfg_dir()))
        open_folder_menu.add_command(label="Конфиги: источники напряжения",
                                     command=lambda: self._open_folder(voltage_source_cfg_dir()))
        open_folder_menu.add_command(label="Конфигурация приложения",
                                     command=lambda: self._open_folder(config_dir()))
        open_folder_mb.configure(menu=open_folder_menu)
        open_folder_mb.pack(side="left", padx=(0, 6))

        ttk.Button(footer_btns, text="Очистить кэш…", command=self._clear_cache_dialog).pack(side="left", padx=(0, 6))

        ttk.Button(footer_btns, text="Даты поверки…", command=self._open_calibration_editor).pack(side="left", padx=(0, 6))
        ttk.Button(footer_btns, text="⚠ Баннер предупреждения", command=self._open_warning_banner).pack(side="left", padx=(0, 6))
        ttk.Button(footer_btns, text="Проверить снова", command=self._run_preflight).pack(side="left")

    def _build_params(self, parent):
        """
        п.33: левая панель разложена по вкладкам (Датчик/Уставка/Приборы/
        Реле), каждая — со своим скроллом (_ScrollableFrame), чтобы
        не обрезаться снизу на небольших экранах. Старт/Стоп/отсчёт и
        галочки безопасности вынесены НАД вкладками — они нужны постоянно,
        прятать их за переключением вкладки было бы неудобно.
        """
        left = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)

        tabs = ttk.Notebook(left)
        tabs.grid(row=0, column=0, sticky="nsew")
        tabs.add(self._build_tab_sensor(tabs), text="Датчик")
        tabs.add(self._build_tab_setpoint(tabs), text="Уставка")
        tabs.add(self._build_tab_instruments(tabs), text="Приборы")
        tabs.add(self._build_tab_relay(tabs), text="Реле")

        bottom = ttk.Frame(left, padding=(2, 10, 2, 0))
        bottom.grid(row=1, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        bottom.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(bottom, text="▶  Старт измерения", style="Accent.TButton",
                                    command=self._start_measurement, state="disabled")
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_btn = ttk.Button(bottom, text="■  Стоп", style="Danger.TButton",
                                   command=self._request_stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        # --- countdown (п.15) — виден только пока идёт измерение ---
        self.countdown_label = ttk.Label(bottom, style="Muted.TLabel", text="")
        self.countdown_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Checkbutton(bottom, text="Игнорировать самотесты (не рекомендуется)",
                        variable=self.skip_selftest_var,
                        command=self._run_preflight).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(bottom, text="Отключить все предупреждения и уведомления (не рекомендуется)",
                        variable=self.suppress_warnings_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 0))
        # Возврат приборов в местное управление после цикла (п.8) — чтобы после
        # измерения можно было пользоваться передней панелью, не передёргивая питание.
        ttk.Checkbutton(bottom, text="Возвращать приборы в Local после измерения",
                        variable=self.restore_local_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self._on_excitation_change()
        self._on_custom_program_change()
        self._on_adaptive_cooling_change()
        self._on_avg_count_change()
        self._on_branch_change()
        self._on_preset_change()
        self._update_sweep_preview()

    # -- вкладка «Уставка»: параметры развёртки, отсечка, усреднение, предпросмотр --
    def _build_tab_setpoint(self, parent):
        scroll = _ScrollableFrame(parent)
        body = scroll.body
        body.columnconfigure(0, weight=1)

        pf = ttk.Labelframe(body, text="Параметры развёртки", padding=10)
        pf.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 8))
        pf.columnconfigure(1, weight=1)

        # Свой сценарий (feature "планировщик кастомных программ", BETA) —
        # переключает между обычным X_start/X_stop/X_step и свободным
        # текстовым DSL (см. sweep.parse_custom_program); branch/preset
        # (вкладка «Дополнительно») в этом режиме тоже прячутся целиком —
        # полярность каждой точки в кастомной программе определяется её
        # буквальным знаком, комбинаторике там нет места (п.33).
        self.custom_program_check = ttk.Checkbutton(
            pf, text="Свой сценарий (BETA)", variable=self.custom_program_var,
            command=self._on_custom_program_change,
        )
        self.custom_program_check.grid(row=0, column=0, columnspan=3, sticky="w")

        self._range_fields_frame = ttk.Frame(pf)
        self._range_fields_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
        self._range_fields_frame.columnconfigure(1, weight=1)
        self.e_start, self.u_start = self._param_row(self._range_fields_frame, 0, "Начало")
        self.e_stop, self.u_stop = self._param_row(self._range_fields_frame, 1, "Конец")
        self.e_step, self.u_step = self._param_row(self._range_fields_frame, 2, "Шаг")
        for entry in (self.e_start, self.e_stop, self.e_step):
            entry.bind("<FocusOut>", lambda e: self._update_sweep_preview())
            entry.bind("<Return>", lambda e: self._update_sweep_preview())

        self._custom_program_frame = ttk.Frame(pf)
        self._custom_program_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
        self._custom_program_frame.columnconfigure(0, weight=1)
        ttk.Label(self._custom_program_frame,
                 text="Точки/диапазоны через запятую — любой порядок, знак, повторы:\n"
                      "число (\"-25\") — одна точка; \"начало:конец:шаг\" — диапазон.\n"
                      "Пример: -25, 0:40:10, -15, +5",
                 style="Muted.TLabel", justify="left").grid(row=0, column=0, sticky="w")
        self.e_custom_program = tk.Text(self._custom_program_frame, height=3, wrap="word")
        self.e_custom_program.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.e_custom_program.bind("<FocusOut>", lambda e: self._update_sweep_preview())
        self.e_custom_program.bind("<KeyRelease>", lambda e: self._update_sweep_preview())

        # V_limit — в своём контейнере, чтобы полностью прятать при
        # возбуждении напряжением, а не просто гасить (п.33: показывать
        # только те поля, что реально будут использованы).
        self._vlimit_frame = ttk.Frame(pf)
        self._vlimit_frame.grid(row=2, column=0, columnspan=3, sticky="ew")
        self._vlimit_frame.columnconfigure(1, weight=1)
        self.e_vlimit, self.u_vlimit = self._param_row(self._vlimit_frame, 0, "Огр. напряжения", unit="В")

        # Огр. тока — симметрично огр. напряжения, только для возбуждения
        # напряжением (защита источника от КЗ/низкоомной нагрузки на выходе,
        # баг-репорт: у уставки тока есть огр. по напряжению, у уставки
        # напряжения должно быть огр. по току).
        self._ilimit_frame = ttk.Frame(pf)
        self._ilimit_frame.grid(row=2, column=0, columnspan=3, sticky="ew")
        self._ilimit_frame.columnconfigure(1, weight=1)
        self.e_ilimit, self.u_ilimit = self._param_row(self._ilimit_frame, 0, "Огр. тока", unit="А")

        # Плавное нарастание (feature, BETA) — только для тока: чекбокс
        # виден только при этом возбуждении (для напряжения прячется целиком,
        # см. _on_excitation_change), взаимно исключает delay/cooling_delay
        # (см. measurement.run_measurement) — ниже переключаются местами.
        self._smooth_ramp_row = ttk.Frame(pf)
        self._smooth_ramp_row.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.smooth_ramp_check = ttk.Checkbutton(
            self._smooth_ramp_row, text="Плавное нарастание (BETA, до 300 А)",
            variable=self.smooth_ramp_var, command=self._on_smooth_ramp_change,
        )
        self.smooth_ramp_check.pack(anchor="w")
        self.smooth_ramp_note_label = ttk.Label(self._smooth_ramp_row, style="Muted.TLabel",
                                                wraplength=300, justify="left")
        self.smooth_ramp_note_label.pack(anchor="w")

        self._delay_cool_frame = ttk.Frame(pf)
        self._delay_cool_frame.grid(row=4, column=0, columnspan=3, sticky="ew")
        self._delay_cool_frame.columnconfigure(1, weight=1)
        self.e_delay, self.u_delay = self._param_row(self._delay_cool_frame, 0, "Задержка установки", unit="с")

        # Адаптивное охлаждение (BETA) — стоит перед самим полем задержки
        # охлаждения (что фиксированной, что мин./макс.), чтобы сразу было
        # видно, что именно переключает эта галочка. Заменяет одну
        # "Задержка охлаждения" двумя явными границами — оператор сам видит
        # и задаёт мин./макс. в секундах, а не множитель от неизвестной
        # итоговой величины (баг-репорт).
        self._adaptive_cooling_row = ttk.Frame(self._delay_cool_frame)
        self._adaptive_cooling_row.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Checkbutton(self._adaptive_cooling_row, text="Адаптивное охлаждение (BETA, растёт с током)",
                        variable=self.adaptive_cooling_var,
                        command=self._on_adaptive_cooling_change).pack(anchor="w")
        ttk.Label(self._adaptive_cooling_row, text="не проверено на реальном стенде",
                  style="Muted.TLabel").pack(anchor="w")

        self._cooling_fixed_frame = ttk.Frame(self._delay_cool_frame)
        self._cooling_fixed_frame.grid(row=2, column=0, columnspan=3, sticky="ew")
        self._cooling_fixed_frame.columnconfigure(1, weight=1)
        self.e_cool, self.u_cool = self._param_row(self._cooling_fixed_frame, 0, "Задержка охлаждения", unit="с")

        self._cooling_adaptive_frame = ttk.Frame(self._delay_cool_frame)
        self._cooling_adaptive_frame.grid(row=2, column=0, columnspan=3, sticky="ew")
        self._cooling_adaptive_frame.columnconfigure(1, weight=1)
        self.e_cool_min, self.u_cool_min = self._param_row(
            self._cooling_adaptive_frame, 0, "Мин. задержка охлаждения", unit="с")
        self.e_cool_min.insert(0, str(DEFAULT_ADAPTIVE_COOLING_MIN_DELAY))
        self.e_cool_max, self.u_cool_max = self._param_row(
            self._cooling_adaptive_frame, 1, "Макс. задержка охлаждения", unit="с")
        self.e_cool_max.insert(0, str(DEFAULT_ADAPTIVE_COOLING_MAX_DELAY))

        self._ramp_duration_frame = ttk.Frame(pf)
        self._ramp_duration_frame.grid(row=4, column=0, columnspan=3, sticky="ew")
        self._ramp_duration_frame.columnconfigure(1, weight=1)
        self.e_ramp_duration, self.u_ramp_duration = self._param_row(
            self._ramp_duration_frame, 0, "Время шага", unit="с")

        ttk.Label(pf, text="Комментарий").grid(row=5, column=0, sticky="w", pady=(6, 0))
        self.e_label = ttk.Entry(pf)
        self.e_label.grid(row=5, column=1, columnspan=2, sticky="ew", pady=(6, 0))

        # Погрешность: брак и отсечка. Баг-репорт п.16: порог погрешности
        # управляет АВТОБРАКОМ точек (пометка Rejected при стабильном выходе за
        # порог, когда заданы I ном. и коэффициент) — независимо от того,
        # останавливать ли из-за этого весь свип. Поэтому порог виден ВСЕГДА, а
        # не только при включённой «остановке» (раньше поле пряталось, а брак
        # всё равно шёл по нему — баг). Число перепромеров (п.12) — рядом.
        err_box = ttk.Labelframe(body, text="Погрешность: брак и отсечка", padding=10)
        err_box.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
        ttk.Label(err_box, text="Порог брака / отсечки, %").grid(row=0, column=0, sticky="w", pady=3)
        self.e_error_threshold = ttk.Entry(err_box, width=8)
        self.e_error_threshold.grid(row=0, column=1, sticky="w", pady=3, padx=(8, 0))
        self.e_error_threshold.insert(0, "1.0")
        ttk.Label(err_box, text="Доп. перепромеров при подозрении на брак").grid(
            row=1, column=0, sticky="w", pady=3)
        self.e_recheck_count = ttk.Entry(err_box, width=8)
        self.e_recheck_count.grid(row=1, column=1, sticky="w", pady=3, padx=(8, 0))
        self.e_recheck_count.insert(0, str(MAX_MEASUREMENT_ATTEMPTS - 1))
        ttk.Label(err_box, text="(0 — без перепромеров, брак сразу)",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(err_box, text="Также останавливать свип при браке",
                        variable=self.stop_on_error_var).grid(
                        row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        avg_box = ttk.Labelframe(body, text="Усреднение", padding=10)
        avg_box.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
        ttk.Label(avg_box, text="Отсчётов на усреднение").grid(row=0, column=0, sticky="w", pady=3)
        self.e_avg_count = ttk.Entry(avg_box, width=6)
        self.e_avg_count.grid(row=0, column=1, sticky="w", pady=3, padx=(8, 0))
        self.e_avg_count.insert(0, str(DEFAULT_AVERAGING_COUNT))
        # Прячем «отбрасывать первый» при одном отсчёте (баг-репорт п.10).
        self.e_avg_count.bind("<KeyRelease>", self._on_avg_count_change)
        self.e_avg_count.bind("<FocusOut>", self._on_avg_count_change)
        ttk.Label(avg_box, text="Задержка между ними, с").grid(row=1, column=0, sticky="w", pady=3)
        self.e_avg_delay = ttk.Entry(avg_box, width=6)
        self.e_avg_delay.grid(row=1, column=1, sticky="w", pady=3, padx=(8, 0))
        self.e_avg_delay.insert(0, str(DEFAULT_AVERAGING_DELAY))
        self.discard_first_check = ttk.Checkbutton(avg_box, text="Отбрасывать первый отсчёт",
                                                   variable=self.discard_first_var)
        self.discard_first_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- предпросмотр развёртки: что реально получится при текущих
        # значениях (не то, что напечатано, а то, что посчитает планировщик,
        # см. _update_sweep_preview) ---
        preview = ttk.Labelframe(body, text="Предпросмотр развёртки", padding=10)
        preview.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.sweep_preview_label = ttk.Label(preview, text="—", style="Muted.TLabel",
                                             wraplength=300, justify="left")
        self.sweep_preview_label.pack(anchor="w", fill="x")

        return scroll

    # -- вкладка «Датчик»: тип возбуждения + метаданные + профиль (п.39-UI) --
    def _build_tab_sensor(self, parent):
        scroll = _ScrollableFrame(parent)
        body = scroll.body
        body.columnconfigure(0, weight=1)

        exc = ttk.Labelframe(body, text="Тип возбуждения", padding=10)
        exc.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 8))
        ttk.Radiobutton(exc, text="Ток (источник тока)", value="current",
                        variable=self.excitation_var, command=self._on_excitation_change).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(exc, text="Напряжение (источник напряжения) — BETA", value="voltage",
                        variable=self.excitation_var, command=self._on_excitation_change).grid(row=1, column=0, sticky="w")

        # --- output type (ось А-1, независимая от возбуждения: чем датчик
        # возбуждают — не то же самое, что и то, что он выдаёт на выходе;
        # действительны любые сочетания — ток/ток, ток/напряжение,
        # напряжение/ток, напряжение/напряжение) ---
        ttk.Separator(exc, orient="horizontal").grid(row=2, column=0, sticky="ew", pady=(8, 6))
        out_row = ttk.Frame(exc)
        out_row.grid(row=3, column=0, sticky="w")
        ttk.Label(out_row, text="Выход датчика:").pack(side="left", padx=(0, 6))
        ttk.Combobox(
            out_row, textvariable=self.output_var, state="readonly", width=10,
            values=["current", "voltage"],
        ).pack(side="left")
        ttk.Label(exc, text="выход «напряжение» — BETA, не проверено на реальном стенде",
                  style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(4, 0))

        meta = ttk.Labelframe(body, text="Метаданные датчика", padding=10)
        meta.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
        meta.columnconfigure(1, weight=1)
        self.l_inom = ttk.Label(meta, text="I ном., А")
        self.l_inom.grid(row=0, column=0, sticky="w", pady=3)
        self.e_inom = ttk.Entry(meta, width=14)
        self.e_inom.grid(row=0, column=1, sticky="ew", pady=3, padx=(8, 0))
        ttk.Label(meta, text="Коэфф. 1:X").grid(row=1, column=0, sticky="w", pady=3)
        self.e_ratio = ttk.Entry(meta, width=14)
        self.e_ratio.grid(row=1, column=1, sticky="ew", pady=3, padx=(8, 0))

        # Смещение нуля (feature): у некоторых датчиков ноль осознанно
        # смещён — вычитается из показания при расчёте погрешности и при
        # живой отсечке по погрешности (см. measurement._measure_point_row,
        # analysis.load_and_analyze). Пусто/0 — поведение как раньше.
        ttk.Label(meta, text="Смещение нуля").grid(row=2, column=0, sticky="w", pady=3)
        self.e_zero_offset = ttk.Entry(meta, width=14)
        self.e_zero_offset.grid(row=2, column=1, sticky="ew", pady=3, padx=(8, 0))

        # Витки не имеют смысла для возбуждения напряжением — прячется в
        # _on_excitation_change (п.33: показывать только нужные поля).
        self._turns_row = ttk.Frame(meta)
        self._turns_row.grid(row=3, column=0, columnspan=2, sticky="ew")
        self._turns_row.columnconfigure(1, weight=1)
        ttk.Label(self._turns_row, text="Витки").grid(row=0, column=0, sticky="w", pady=3)
        self.e_turns = ttk.Entry(self._turns_row, width=14)
        self.e_turns.grid(row=0, column=1, sticky="ew", pady=3, padx=(8, 0))
        self.e_turns.insert(0, "1")

        cfg = ttk.Labelframe(body, text="Профиль датчика", padding=10)
        cfg.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        cfg.columnconfigure(1, weight=1)
        ttk.Label(cfg, text="Имя").grid(row=0, column=0, sticky="w", pady=3)
        self.e_config_name = ttk.Combobox(cfg, state="normal")
        self.e_config_name.grid(row=0, column=1, sticky="ew", pady=3, padx=(8, 0))
        btn_frame = ttk.Frame(cfg)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btn_frame, text="Сохранить", command=self._save_config).pack(side="left", padx=(0, 6))
        ttk.Button(btn_frame, text="Загрузить", command=self._load_config).pack(side="left", padx=(0, 6))
        ttk.Button(btn_frame, text="Переименовать", command=self._rename_config).pack(side="left", padx=(0, 6))
        ttk.Button(btn_frame, text="Удалить", command=self._delete_config).pack(side="left")

        return scroll

    # -- вкладка «Приборы»: адреса (п.12) + мигание (п.11) + скан (п.25) --
    def _build_tab_instruments(self, parent):
        scroll = _ScrollableFrame(parent)
        body = scroll.body
        body.columnconfigure(0, weight=1)

        adv = ttk.Labelframe(body, text="Приборы (необязательно, иначе автопоиск)", padding=10)
        adv.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 10))
        adv.columnconfigure(1, weight=1)
        self.e_dmm, self._blink_btn_dmm = self._combo_addr_row(adv, 0, "Мультиметр VISA")
        self._blink_btn_dmm.configure(command=lambda: self._do_blink('dmm'))
        self.e_src, self._blink_btn_src = self._combo_addr_row(adv, 1, "Источник VISA")
        self._blink_btn_src.configure(command=lambda: self._do_blink('src'))

        self.discovery_status_label = ttk.Label(adv, style="Muted.TLabel", text="Поиск приборов…",
                                                 wraplength=320, justify="left")
        self.discovery_status_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(adv, text="Обновить список", command=self._rescan_discovery_now).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        return scroll

    # -- вкладка «Реле»: порт платы + направление развёртки --
    def _build_tab_relay(self, parent):
        scroll = _ScrollableFrame(parent)
        body = scroll.body
        body.columnconfigure(0, weight=1)

        port_box = ttk.Labelframe(body, text="Плата реле", padding=10)
        port_box.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 8))
        port_box.columnconfigure(1, weight=1)
        self.e_relay = self._addr_row(port_box, 0, "Порт реле (COMx)")

        self._dir_box = ttk.Labelframe(body, text="Направление развёртки", padding=10)
        self._dir_box.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
        ttk.Label(self._dir_box, text="Полярность").grid(row=0, column=0, sticky="w", pady=3)
        self.branch_combo = ttk.Combobox(self._dir_box, textvariable=self.branch_var, state="readonly", width=10,
                                         values=[b.value for b in Branch])
        self.branch_combo.grid(row=0, column=1, sticky="w", pady=3, padx=(8, 0))
        self.branch_combo.bind("<<ComboboxSelected>>",
                               lambda e: (self._on_branch_change(), self._update_sweep_preview()))

        # Схема прохода имеет смысл только при branch=both — прячется,
        # когда снимается только одна полярность (п.33).
        self._preset_row = ttk.Frame(self._dir_box)
        self._preset_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        preset_line = ttk.Frame(self._preset_row)
        preset_line.pack(side="top", fill="x")
        ttk.Label(preset_line, text="Схема прохода").pack(side="left")
        preset_combo = ttk.Combobox(preset_line, textvariable=self.preset_var, state="readonly", width=10,
                                    values=[p.value for p in DirectionPreset])
        preset_combo.pack(side="left", padx=(8, 0))
        preset_combo.bind("<<ComboboxSelected>>",
                          lambda e: (self._on_preset_change(), self._update_sweep_preview()))
        # Коротко, что значит выбранная схема — оператору не приходится
        # держать в голове разницу diverging/converging/descending/full_cycle.
        self.preset_desc_label = ttk.Label(self._preset_row, style="Muted.TLabel",
                                           wraplength=300, justify="left")
        self.preset_desc_label.pack(side="top", anchor="w", pady=(2, 0))
        # Плавный проход нуля (п.18) — только для петли гистерезиса (FULL_CYCLE);
        # видимость чекбокса переключается в _on_preset_change.
        self.zero_crossing_check = ttk.Checkbutton(
            self._preset_row, text="Плавный проход нуля (медленно, без скачка)",
            variable=self.zero_crossing_smooth_var,
            command=self._update_sweep_preview)
        self.zero_crossing_check.pack(side="top", anchor="w", pady=(2, 0))

        return scroll

    def _param_row(self, parent, row, label, unit=""):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent, width=12)
        entry.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 6))
        unit_lbl = ttk.Label(parent, text=unit, style="Muted.TLabel", width=3)
        unit_lbl.grid(row=row, column=2, sticky="w", pady=3)
        return entry, unit_lbl

    def _addr_row(self, parent, row, label):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        entry = ttk.Entry(parent)
        entry.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 0))
        return entry

    def _combo_addr_row(self, parent, row, label):
        """
        Как _addr_row, но выпадающий список (п.12) вместо пустого поля —
        заполняется найденными приборами (см. _refresh_discovery_ui). Поле
        остаётся редактируемым (state="normal", не "readonly"): скан мог
        ещё не найти нужный прибор, и поручать оператору набрать адрес
        руками в этом случае — не регресс, а разумный запасной путь.
        Рядом — кнопка «blink» (BETA, п.11).
        """
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        combo = ttk.Combobox(parent, state="normal")
        combo.grid(row=row, column=1, sticky="ew", pady=3, padx=(8, 6))
        blink_btn = ttk.Button(parent, text="blink (BETA)", width=13)
        blink_btn.grid(row=row, column=2, pady=3)
        return combo, blink_btn

    @staticmethod
    def _combo_address(combo) -> str:
        """
        Значение поля может быть либо адресом, набранным вручную, либо
        строкой из выпадающего списка вида "АДРЕС — конфиг" (см.
        _refresh_discovery_ui) — оттуда нужен только сам адрес.
        """
        text = combo.get().strip()
        if not text:
            return ""
        return text.split(" — ", 1)[0].strip()

    def _build_right(self, parent):
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        nb = ttk.Notebook(right)
        nb.grid(row=0, column=0, sticky="nsew")

        # --- log tab ---
        log_frame = ttk.Frame(nb, padding=2)
        nb.add(log_frame, text="  Журнал  ")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(log_frame, wrap="word", height=10,
                                             font=("Consolas", 9), bg="#0f172a", fg="#e2e8f0",
                                             insertbackground="#e2e8f0", relief="flat", padx=10, pady=8)
        self.log.grid(row=0, column=0, sticky="nsew")
        self.log.configure(state="disabled")

        # --- plot tab ---
        plot_tab = ttk.Frame(nb, padding=8)
        nb.add(plot_tab, text="  Данные  ")
        plot_tab.columnconfigure(0, weight=1)
        # Таблица точек — единственный блок здесь, которому есть смысл
        # расти: свободного места снизу стало много после переноса самого
        # графика на отдельную вкладку (баг-репорт), пусть забирает его.
        plot_tab.rowconfigure(3, weight=1)

        # -- файл (п.20: график из произвольного CSV, по умолчанию последний) --
        file_bar = ttk.Frame(plot_tab)
        file_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(file_bar, text="Открыть CSV…", command=self._open_csv_for_plot).pack(side="left")
        self.plot_file_label = ttk.Label(file_bar, style="Muted.TLabel",
                                         text="файл не выбран — по умолчанию последний в папке данных")
        self.plot_file_label.pack(side="left", padx=(10, 0))

        # -- параметры анализа --
        an = ttk.Labelframe(plot_tab, text="Параметры анализа", padding=8)
        an.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.l_plot_inom = ttk.Label(an, text="I ном., А")
        self.l_plot_inom.grid(row=0, column=0, sticky="w")
        self.plot_e_inom = ttk.Entry(an, width=10)
        self.plot_e_inom.grid(row=0, column=1, padx=(6, 14))
        ttk.Label(an, text="Коэфф. 1:X").grid(row=0, column=2, sticky="w")
        self.plot_e_ratio = ttk.Entry(an, width=10)
        self.plot_e_ratio.grid(row=0, column=3, padx=(6, 14))
        ttk.Button(an, text="Построить график", command=self._do_analyze).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(an, text="Определить коэфф. (BETA)", command=self._do_estimate_ratio).grid(row=0, column=5)

        # -- отображение: подписи погрешности (п.30) + диапазоны осей (п.36) --
        disp = ttk.Labelframe(plot_tab, text="Отображение", padding=8)
        disp.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        ttk.Checkbutton(disp, text="Подписи погрешности над точками",
                        variable=self.show_labels_var).grid(row=0, column=0, columnspan=6, sticky="w")

        ttk.Checkbutton(disp, text="Авто-диапазон осей", variable=self.auto_range_var,
                        command=self._on_auto_range_change).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

        # Поля диапазонов нефункциональны при включённом авто-диапазоне —
        # прячем их целиком (не просто блокируем), чтобы не вводить в
        # заблуждение видом рабочих на самом деле полей (баг-репорт, п.33).
        self._range_fields_row = ttk.Frame(disp)
        self._range_fields_row.grid(row=2, column=0, columnspan=6, sticky="ew")
        ttk.Label(self._range_fields_row, text="X мин/макс").grid(row=0, column=0, sticky="w", pady=(4, 0))
        self.e_xmin = ttk.Entry(self._range_fields_row, width=8); self.e_xmin.grid(row=0, column=1, padx=(4, 4))
        self.e_xmax = ttk.Entry(self._range_fields_row, width=8); self.e_xmax.grid(row=0, column=2, padx=(0, 14))
        ttk.Label(self._range_fields_row, text="Y выход мин/макс").grid(row=0, column=3, sticky="w")
        self.e_y1min = ttk.Entry(self._range_fields_row, width=8); self.e_y1min.grid(row=0, column=4, padx=(4, 4))
        self.e_y1max = ttk.Entry(self._range_fields_row, width=8); self.e_y1max.grid(row=0, column=5, padx=(0, 14))
        ttk.Label(self._range_fields_row, text="Y погр.,% мин/макс").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.e_y2min = ttk.Entry(self._range_fields_row, width=8); self.e_y2min.grid(row=1, column=1, padx=(4, 4), pady=(4, 0))
        self.e_y2max = ttk.Entry(self._range_fields_row, width=8); self.e_y2max.grid(row=1, column=2, padx=(0, 14), pady=(4, 0))
        self._on_auto_range_change()

        # -- правка точек (п.26): исключение не удаляет данные, только помечает --
        pts = ttk.Labelframe(plot_tab, text="Точки", padding=8)
        pts.grid(row=3, column=0, sticky="nsew", pady=(0, 6))
        pts.columnconfigure(0, weight=1)
        pts.rowconfigure(0, weight=1)
        columns = ("x", "i_meas", "error", "rejected", "excluded")
        self.points_tree = ttk.Treeview(pts, columns=columns, show="headings", height=16, selectmode="extended")
        for col, text, width in (
            ("x", "Возбуждение", 100), ("i_meas", "I изм., А", 90), ("error", "Погр., %", 90),
            ("rejected", "Брак (авто)", 90), ("excluded", "Исключена", 90),
        ):
            self.points_tree.heading(col, text=text)
            self.points_tree.column(col, width=width, anchor="center")
        self.points_tree.grid(row=0, column=0, columnspan=4, sticky="nsew")
        pts_scroll = ttk.Scrollbar(pts, orient="vertical", command=self.points_tree.yview)
        self.points_tree.configure(yscrollcommand=pts_scroll.set)
        pts_scroll.grid(row=0, column=4, sticky="ns")

        pts_btns = ttk.Frame(pts)
        pts_btns.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Button(pts_btns, text="Исключить выбранные",
                  command=lambda: self._toggle_selected_points(True)).pack(side="left", padx=(0, 6))
        ttk.Button(pts_btns, text="Вернуть выбранные",
                  command=lambda: self._toggle_selected_points(False)).pack(side="left", padx=(0, 6))
        ttk.Button(pts_btns, text="Сохранить и перестроить",
                  command=self._save_point_exclusions).pack(side="left", padx=(0, 14))
        ttk.Button(pts_btns, text="Инвертировать вход… (не рекомендуется)",
                  command=self._do_invert_input).pack(side="left")

        # -- экспорт (п.21) --
        exp = ttk.Frame(plot_tab)
        exp.grid(row=4, column=0, sticky="w", pady=(0, 6))
        ttk.Button(exp, text="Экспорт в XLSX…", command=self._do_export_xlsx).pack(side="left")

        # -- вкладка «График»: только сам холст (Данные выше — файл, точки,
        # параметры анализа) — так графику остаётся всё свободное место (баг-
        # репорт: на "Данные" для него уже не было места), а после построения
        # (см. _do_analyze/_auto_plot_after_measurement) оператора сразу
        # переключает сюда. --
        graph_tab = ttk.Frame(nb, padding=8)
        nb.add(graph_tab, text="  График  ")
        graph_tab.rowconfigure(0, weight=1)
        graph_tab.columnconfigure(0, weight=1)
        self.plot_frame = ttk.Frame(graph_tab, style="Card.TFrame")
        self.plot_frame.grid(row=0, column=0, sticky="nsew")
        self.plot_frame.rowconfigure(0, weight=1)
        self.plot_frame.columnconfigure(0, weight=1)
        self.plot_hint = ttk.Label(self.plot_frame, style="Muted.TLabel",
                                   text="После измерения задайте I ном. и X, затем «Построить график».\n"
                                        "Либо откройте любой CSV кнопкой выше.")
        self.plot_hint.grid(row=0, column=0)

        self._build_manual_tab(nb)

        self.notebook = nb

    # ---------------------------------------------------------- manual control
    def _build_manual_tab(self, nb):
        """
        Вкладка «Ручное управление» (Ф4, п.13 — реле напрямую, п.40 — прямая
        знаковая уставка). Отдельно от измерительного цикла: сессия
        открывается один раз («Открыть сессию») и держится, пока оператор
        не закроет её или не нажмёт аварийный «СТОП» — тот же путь
        безопасности (SessionHandle.emergency_stop), что и у измерения.
        """
        outer = ttk.Frame(nb, padding=2)
        nb.add(outer, text="  Ручное управление  ")
        outer.rowconfigure(0, weight=1)
        outer.columnconfigure(0, weight=1)
        scroll = _ScrollableFrame(outer)
        scroll.grid(row=0, column=0, sticky="nsew")
        tab = ttk.Frame(scroll.body, padding=10)
        tab.grid(row=0, column=0, sticky="ew")
        tab.columnconfigure(0, weight=1)

        warn = ttk.Label(
            tab, style="Muted.TLabel", wraplength=520, justify="left",
            text="Вне измерительного цикла: держит уставку/положение реле до явной остановки. "
                 "Уважает лимиты платы реле (жёсткий запрет 800 А) и аварийный останов.",
        )
        warn.grid(row=0, column=0, sticky="w", pady=(0, 10))

        session_bar = ttk.Frame(tab)
        session_bar.grid(row=1, column=0, sticky="w", pady=(0, 10))
        self.manual_open_btn = ttk.Button(session_bar, text="Открыть сессию", command=self._manual_open_session)
        self.manual_open_btn.pack(side="left", padx=(0, 6))
        self.manual_close_btn = ttk.Button(session_bar, text="Закрыть сессию",
                                           command=self._manual_close_session, state="disabled")
        self.manual_close_btn.pack(side="left")

        relay_box = ttk.Labelframe(tab, text="Реле напрямую", padding=10)
        relay_box.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.manual_relay_buttons = []
        for text, direction in (("Прямо (IFW)", "forward"), ("Обратно (IRW)", "reverse"), ("Выкл (I_0)", "off")):
            btn = ttk.Button(relay_box, text=text, state="disabled",
                             command=lambda d=direction: self._manual_set_relay(d))
            btn.pack(side="left", padx=(0, 6))
            self.manual_relay_buttons.append(btn)

        setpoint_box = ttk.Labelframe(tab, text="Прямая уставка (со знаком)", padding=10)
        setpoint_box.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(setpoint_box, text="Значение").grid(row=0, column=0, sticky="w")
        self.e_manual_setpoint = ttk.Entry(setpoint_box, width=12, state="disabled")
        self.e_manual_setpoint.grid(row=0, column=1, sticky="w", padx=(8, 6))
        self.manual_apply_btn = ttk.Button(setpoint_box, text="Применить", state="disabled",
                                           command=self._manual_apply_setpoint)
        self.manual_apply_btn.grid(row=0, column=2, padx=(0, 6))
        self.manual_stop_btn = ttk.Button(setpoint_box, text="Остановить", state="disabled",
                                          command=self._manual_stop)
        self.manual_stop_btn.grid(row=0, column=3)
        ttk.Label(setpoint_box, text="(> 0 — прямое направление, < 0 — обратное, 0 — выключить)",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self.manual_status_label = ttk.Label(tab, style="Muted.TLabel", text="Сессия не открыта.")
        self.manual_status_label.grid(row=4, column=0, sticky="w")

    # ---------------------------------------------------------------- helpers
    def _prefill_from_config(self):
        saved = self.config_mgr.load()
        if not saved:
            return
        if saved.get("excitation_type") in ("current", "voltage"):
            self.excitation_var.set(saved["excitation_type"])
        if saved.get("output_type") in ("current", "voltage"):
            self.output_var.set(saved["output_type"])
        mapping = {
            "X_start": self.e_start, "X_stop": self.e_stop, "X_step": self.e_step,
            "V_limit": self.e_vlimit, "I_limit": self.e_ilimit, "delay": self.e_delay, "cooling_delay": self.e_cool,
            "label": self.e_label,
        }
        for key, entry in mapping.items():
            val = saved.get(key)
            if val is not None:
                entry.delete(0, "end")
                entry.insert(0, str(val))
        self._on_excitation_change()

    def _on_excitation_change(self):
        is_current = self.excitation_var.get() == "current"
        unit = "А" if is_current else "В"
        for lbl in (self.u_start, self.u_stop, self.u_step):
            lbl.configure(text=unit)
        # Баг-репорт: раньше подпись номинала всегда была "I ном., А", даже
        # при возбуждении напряжением — не соответствовало смыслу поля.
        self.l_inom.configure(text="I ном., А" if is_current else "U ном., В")
        # п.33: показываем только то, что реально будет использовано —
        # огр. напряжения и витки не имеют смысла при возбуждении
        # напряжением (там вместо огр. напряжения — симметричное огр. тока),
        # поэтому прячутся целиком, а не просто гаснут.
        if is_current:
            self._vlimit_frame.grid()
            self._ilimit_frame.grid_remove()
            self._turns_row.grid()
        else:
            self._vlimit_frame.grid_remove()
            self._ilimit_frame.grid()
            self._turns_row.grid_remove()
        self._update_smooth_ramp_row_visibility()
        self._on_smooth_ramp_change()
        self._refresh_profile_list()
        self._update_sweep_preview()

    def _update_smooth_ramp_row_visibility(self):
        """
        Плавное нарастание — только для тока (см. measurement.py) и, отдельно,
        не в своём сценарии (баг-репорт: слишком много непроверенных
        сочетаний с произвольным порядком/знаком точек — своя программа и
        так предназначена для точечных случаев, а не для гладких переходов).
        В обоих случаях чекбокс прячется целиком, а не просто гаснет
        (п.33), и режим форсированно выключается, чтобы не остался
        незаметно включённым.
        """
        if not hasattr(self, '_smooth_ramp_row'):
            return
        allowed = self.excitation_var.get() == "current" and not self.custom_program_var.get()
        if allowed:
            self._smooth_ramp_row.grid()
        else:
            self._smooth_ramp_row.grid_remove()
            self.smooth_ramp_var.set(False)

    def _on_custom_program_change(self):
        """
        Свой сценарий (feature "планировщик кастомных программ", BETA) —
        заменяет X_start/X_stop/X_step свободным текстовым DSL (см.
        sweep.parse_custom_program). branch/preset (вкладка «Направление
        развёртки») в этом режиме не участвуют вовсе — полярность каждой
        точки определяется её буквальным знаком, а не комбинаторикой,
        поэтому вкладка прячется целиком, а не просто гаснет (п.33).

        Плавное нарастание и адаптивное охлаждение тоже прячутся целиком
        (см. _update_smooth_ramp_row_visibility/_update_adaptive_cooling_row_visibility) —
        обе непроверенные BETA-функции рассчитаны на предсказуемую
        монотонную развёртку, а свой сценарий по смыслу — для тех редких
        точечных случаев, где оператору нужно что-то конкретное, а не
        гладкий проход (баг-репорт). Отсечку по погрешности НЕ прячем —
        она нужна независимо от того, как построена развёртка.
        """
        custom = self.custom_program_var.get()
        if custom:
            self._range_fields_frame.grid_remove()
            self._custom_program_frame.grid()
        else:
            self._range_fields_frame.grid()
            self._custom_program_frame.grid_remove()
        if hasattr(self, '_dir_box'):
            if custom:
                self._dir_box.grid_remove()
            else:
                self._dir_box.grid()
        self._update_smooth_ramp_row_visibility()
        # _update_smooth_ramp_row_visibility() могла форсированно сбросить
        # smooth_ramp_var — без этого вызова _delay_cool_frame/
        # _ramp_duration_frame остались бы в прежнем (уже неверном)
        # состоянии видимости (баг: поля задержки/охлаждения пропадали
        # навсегда, если до этого было включено плавное нарастание).
        self._on_smooth_ramp_change()
        self._update_adaptive_cooling_row_visibility()
        self._update_sweep_preview()

    def _update_adaptive_cooling_row_visibility(self):
        """Адаптивное охлаждение прячется в своём сценарии — см. _on_custom_program_change."""
        if not hasattr(self, '_adaptive_cooling_row'):
            return
        if self.custom_program_var.get():
            self._adaptive_cooling_row.grid_remove()
            if self.adaptive_cooling_var.get():
                self.adaptive_cooling_var.set(False)
                self._on_adaptive_cooling_change()
        else:
            self._adaptive_cooling_row.grid()

    def _on_adaptive_cooling_change(self):
        """
        Адаптивное охлаждение (BETA, галочка на вкладке «Уставка») —
        заменяет одну "Задержка охлаждения" двумя явными границами (мин./
        макс. в секундах), которые оператор задаёт сам; функция сама
        интерполирует между ними по току (см.
        measurement._adaptive_cooling_delay). Показываем только то, что
        реально будет использовано (п.33).
        """
        if not hasattr(self, '_cooling_fixed_frame'):
            return
        if self.adaptive_cooling_var.get():
            self._cooling_fixed_frame.grid_remove()
            self._cooling_adaptive_frame.grid()
        else:
            self._cooling_fixed_frame.grid()
            self._cooling_adaptive_frame.grid_remove()

    def _on_smooth_ramp_change(self):
        """
        Плавное нарастание (BETA) взаимно исключает delay/cooling_delay и
        адаптивное охлаждение (см. measurement.run_measurement) — при
        включении прячем эти поля целиком, показываем время шага (п.33:
        показывать только то, что реально будет использовано).
        """
        smooth = self.excitation_var.get() == "current" and self.smooth_ramp_var.get()
        if smooth:
            self._delay_cool_frame.grid_remove()
            self._ramp_duration_frame.grid()
        else:
            self._delay_cool_frame.grid()
            self._ramp_duration_frame.grid_remove()
        self._update_sweep_preview()

    def _refresh_profile_list(self):
        """п.39-UI: список профилей датчиков в выпадающем списке зависит от текущего типа возбуждения."""
        names = self.sensor_config_mgr.list_sensor_configs(excitation_type=self.excitation_var.get())
        self.e_config_name['values'] = names

    def _on_avg_count_change(self, *_):
        """
        Баг-репорт п.10: галочка «отбрасывать первый отсчёт» бессмысленна при
        одном отсчёте (нечего отбрасывать) — прячем её, когда отсчётов ≤ 1.
        """
        if not hasattr(self, 'discard_first_check'):
            return
        raw = self.e_avg_count.get().strip().replace(",", ".")
        try:
            count = int(float(raw)) if raw else DEFAULT_AVERAGING_COUNT
        except ValueError:
            count = DEFAULT_AVERAGING_COUNT
        if count <= 1:
            self.discard_first_check.grid_remove()
        else:
            self.discard_first_check.grid()

    def _on_branch_change(self):
        """п.33: схема прохода имеет смысл только при полярности «both»."""
        if self.branch_var.get() == Branch.BOTH.value:
            self._preset_row.grid()
        else:
            self._preset_row.grid_remove()

    _PRESET_DESCRIPTIONS = {
        DirectionPreset.DIVERGING.value: "0 → +X, затем 0 → −X (по умолчанию)",
        DirectionPreset.CONVERGING.value: "+X → 0 → −X, без остановки в нуле",
        DirectionPreset.DESCENDING.value: "+X → 0 и −X → 0 — обе ветви идут к нулю",
        DirectionPreset.FULL_CYCLE.value: "0 → +X → 0 → −X → 0 — петля гистерезиса "
                                          "(можно включить плавный проход нуля)",
    }

    def _update_zero_crossing_visibility(self):
        """Галочка плавного прохода нуля (п.18) видна только для FULL_CYCLE."""
        if not hasattr(self, 'zero_crossing_check'):
            return
        if self.preset_var.get() == DirectionPreset.FULL_CYCLE.value:
            self.zero_crossing_check.pack(side="top", anchor="w", pady=(2, 0))
        else:
            self.zero_crossing_check.pack_forget()

    def _on_preset_change(self):
        """
        п.9 (баг-репорт): короткая наглядная расшифровка выбранной схемы
        прохода.

        Баг-репорт: если введённый диапазон уже сам по себе двуполярный
        (например 150 -> -150) — preset ни на что не влияет (см.
        sweep.plan_sweep/preset_applies), измерение идёт буквальным
        проходом между X_start и X_stop, полностью игнорируя выбранную
        схему. Раньше это никак не показывалось: выпадающий список и его
        описание выглядели так, будто пресет применится, а по факту нет.
        Здесь та же самая логика (sweep.preset_applies), что использует и
        сам план измерения — предупреждение не может разойтись с тем, что
        реально будет измерено.
        """
        if not hasattr(self, 'preset_desc_label'):
            return
        self._update_zero_crossing_visibility()
        try:
            x_start = float(self.e_start.get().strip().replace(",", "."))
            x_stop = float(self.e_stop.get().strip().replace(",", "."))
            branch = Branch(self.branch_var.get())
        except ValueError:
            self.preset_desc_label.configure(
                text=self._PRESET_DESCRIPTIONS.get(self.preset_var.get(), ""),
                foreground=MUTED)
            return
        if branch == Branch.BOTH and not preset_applies(x_start, x_stop, branch):
            self.preset_desc_label.configure(
                text=(f"⚠ {x_start:+g} → {x_stop:+g} уже охватывает обе полярности напрямую — "
                      f"схема прохода не применяется, идёт буквальный проход между ними"),
                foreground=BUSY_COLOR)
        else:
            self.preset_desc_label.configure(
                text=self._PRESET_DESCRIPTIONS.get(self.preset_var.get(), ""),
                foreground=MUTED)

    def _set_branch_safe(self, raw):
        """
        Загруженный профиль датчика мог быть сохранён другой версией/руками
        отредактирован — некорректное значение в readonly Combobox иначе
        просто повисло бы как нечитаемая строка вместо того, чтобы выпадающий
        список показывал реальный выбор. Откатываемся на дефолт и явно
        говорим об этом в журнале — то самое "если прога скорректировала
        значение, оператор должен увидеть, что именно она выбрала".
        """
        try:
            Branch(raw)
            self.branch_var.set(raw)
        except ValueError:
            self.branch_var.set(Branch.BOTH.value)
            self._append_log(f"Предупреждение: некорректная полярность в конфиге ('{raw}'), использовано 'both'.\n")

    def _set_preset_safe(self, raw):
        try:
            DirectionPreset(raw)
            self.preset_var.set(raw)
        except ValueError:
            self.preset_var.set(DirectionPreset.DIVERGING.value)
            self._append_log(
                f"Предупреждение: некорректная схема прохода в конфиге ('{raw}'), использована 'diverging'.\n")

    def _update_sweep_preview(self):
        """
        Показывает, что РЕАЛЬНО получится при текущих значениях полей —
        считается тем же планировщиком (sweep.plan_sweep), что и сам
        измерительный цикл, а не отдельной, потенциально расходящейся
        копией той же логики. Отвечает на «а что на самом деле произойдёт»
        сразу, без необходимости запускать измерение, чтобы это увидеть —
        включая случаи, когда ввод скорректирован программой (например,
        обе полярности сходятся в одном общем нуле для diverging/converging).
        """
        if not hasattr(self, 'sweep_preview_label'):
            return

        # Держим предупреждение о "молча игнорируемом пресете" в актуальном
        # состоянии при любом изменении, приводящем сюда (начало/конец,
        # полярность, сам пресет) — см. _on_preset_change.
        self._on_preset_change()

        is_custom = self.custom_program_var.get()
        if is_custom:
            text = self.e_custom_program.get("1.0", "end")
            try:
                plan = plan_custom_sweep(text)
            except Exception as e:
                self.sweep_preview_label.configure(text=f"— {e} —")
                self._update_smooth_ramp_availability(None)
                return
        else:
            try:
                x_start = float(self.e_start.get().strip().replace(",", "."))
                x_stop = float(self.e_stop.get().strip().replace(",", "."))
                x_step = float(self.e_step.get().strip().replace(",", "."))
                if x_step <= 0:
                    raise ValueError
                branch = Branch(self.branch_var.get())
                preset = DirectionPreset(self.preset_var.get())
                plan = plan_sweep(x_start, x_stop, x_step, branch=branch, preset=preset)
            except Exception:
                self.sweep_preview_label.configure(text="— заполните начало/конец/шаг корректными числами —")
                self._update_smooth_ramp_availability(None)
                return

        if not plan:
            self.sweep_preview_label.configure(text="— развёртка пуста —")
            self._update_smooth_ramp_availability(None)
            return

        unit = "А" if self.excitation_var.get() == "current" else "В"
        values = [p.x_set for p in plan]
        if is_custom:
            # Свой сценарий (feature): полный список точек, без обрезки —
            # оператор сам написал каждую из них и должен видеть их все
            # (в отличие от обычной развёртки, где точек может быть много
            # и капать до первых 6 достаточно).
            shown = " → ".join(f"{v:+g}" if v != 0 else "0" for v in values)
        else:
            shown = " → ".join(f"{v:+g}" if v != 0 else "0" for v in values[:6])
            if len(values) > 6:
                shown += " → …"
        self.sweep_preview_label.configure(text=f"Точек: {len(plan)}  ·  {shown}  {unit}")

        max_magnitude = max((p.magnitude for p in plan), default=0.0)
        self._update_smooth_ramp_availability(max_magnitude if self.excitation_var.get() == "current" else None)

    def _update_smooth_ramp_availability(self, max_current):
        """
        Плавное нарастание (BETA) выше limits.SMOOTH_RAMP_WARN_CURRENT_A —
        теперь это ПРЕДУПРЕЖДЕНИЕ, а не запрет (баг-репорт: раньше чекбокс
        блокировался целиком). Чекбокс всегда кликабелен; при большом токе
        рядом показывается предупреждающая заметка, а сам факт ещё раз
        всплывёт в диалоге подтверждения старта (см. _start_measurement).
        """
        if not hasattr(self, 'smooth_ramp_check'):
            return
        self.smooth_ramp_check.configure(state="normal")
        warn = smooth_ramp_warning(max_current) if max_current is not None else None
        # Заметку показываем только когда режим включён (иначе она сбивает с
        # толку на выключенной функции) и ток реально выше порога.
        if warn and self.smooth_ramp_var.get():
            self.smooth_ramp_note_label.configure(
                text=f"⚠ {max_current:.1f} А выше {SMOOTH_RAMP_WARN_CURRENT_A:.0f} А — "
                     "алгоритм на таких токах не проверялся, на ответственности оператора.",
                foreground=BUSY_COLOR)
        else:
            self.smooth_ramp_note_label.configure(text="", foreground=MUTED)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text, kind="muted"):
        color = {"ok": OK_COLOR, "error": ERR_COLOR, "busy": BUSY_COLOR}.get(kind, MUTED)
        self.status_label.configure(text=text, foreground=color)
        self.status_dot.itemconfigure(self._dot, fill=color)

    def _on_tk_callback_exception(self, exc_type, exc_value, exc_tb):
        """
        Единый обработчик необработанных исключений в Tk-колбэках (см. __init__,
        report_callback_exception). Пишет полную трассировку в файл-лог и
        показывает оператору короткое сообщение — вместо прежнего молчаливого
        проглатывания, из-за которого «вылеты» разных функций были без следа.
        Сам обработчик не должен падать (иначе рекурсия), поэтому всё в try.
        """
        try:
            _log.error("Необработанное исключение в Tk-колбэке",
                       exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            pass
        try:
            traceback.print_exception(exc_type, exc_value, exc_tb)
        except Exception:
            pass
        try:
            messagebox.showerror(
                "Внутренняя ошибка",
                f"{exc_type.__name__}: {exc_value}\n\nПодробности записаны в журнал приложения.")
        except Exception:
            pass

    # --------------------------------------------------------------- preflight
    def _run_preflight(self):
        self._preflight_ok = False
        self.start_btn.configure(state="disabled")
        self._set_status("Проверка NI-VISA и самотестов…", "busy")
        self.footer_label.configure(text="Идёт предполётная проверка…")
        threading.Thread(target=self._preflight_worker, daemon=True).start()

    def _preflight_worker(self):
        try:
            from visa_backend import check_visa
            visa = check_visa()
            self.events.put(("log", "[VISA] " + visa.summary_line() + "\n"))
            if not visa.ok:
                self.events.put(("preflight", (False, "NI-VISA не найдена", visa.message, None, None)))
                return

            if self.skip_selftest_var.get():
                suffix = " · самотесты пропущены"
                self.events.put(("preflight", (True, visa.summary_line() + suffix, visa.message,
                                                visa.backend, suffix)))
                return

            from selftest import run_selftests
            self.events.put(("log", "[Самотесты] запуск виртуальной проверки кода…\n"))
            st = run_selftests()
            self.events.put(("log", f"[Самотесты] {st.summary}\n"))
            suffix = " · самотесты OK" if st.ok else " · САМОТЕСТЫ ПРОВАЛЕНЫ"
            status = visa.summary_line() + suffix
            self.events.put(("preflight", (st.ok, status, st.output if not st.ok else visa.message,
                                            visa.backend if st.ok else None, suffix if st.ok else None)))
        except Exception as e:
            self.events.put(("preflight", (False, "Ошибка проверки", str(e), None, None)))

    # -------------------------------------------------------------- discovery
    def _refresh_discovery_ui(self):
        """
        Периодический опрос снимка DiscoveryService (п.25) — так же, как
        _drain_events опрашивает очередь измерения: сервис живёт в своём
        потоке и никогда не трогает виджеты напрямую, только через этот
        цикл на главном потоке Tk.
        """
        if self._closing:
            return
        self._apply_discovery_state(self.discovery.snapshot())
        self._discovery_after_id = self.root.after(1000, self._refresh_discovery_ui)

    def _apply_discovery_state(self, state):
        dmm_kind = 'multimeter' if self.output_var.get() == 'current' else 'voltmeter'
        src_kind = 'current_source' if self.excitation_var.get() == 'current' else 'voltage_source'

        # Текущее значение поля не затираем (оператор мог начать печатать
        # адрес руками) — обновляем только список выпадающих вариантов.
        self.e_dmm['values'] = [i.label for i in state.by_kind(dmm_kind)]
        self.e_src['values'] = [i.label for i in state.by_kind(src_kind)]

        if state.scanning:
            status = "Идёт поиск приборов…"
        elif state.last_scan_error:
            status = f"Поиск приборов не удался: {state.last_scan_error}"
        else:
            status = f"Найдено VISA-приборов: {len(state.instruments)}"
        status += "  ·  реле: " + (f"{state.relay_port}" if state.relay_port else "не найдено")
        self.discovery_status_label.configure(text=status)

        # "No Relay" (feature): без платы реле positive/negative/both
        # физически невыполнимы (нечем коммутировать) — полярность
        # заблокирована на единственном исполнимом значении, а не просто
        # "предложена". Разблокируется, как только сервис обнаружения
        # снова видит плату — но НЕ откатывает выбор автоматически назад
        # (оператор сам решает, переключаться ли, см. остальные автокоррекции).
        relay_found = bool(state.relay_port)
        if not relay_found and not self._branch_locked_no_relay:
            self._branch_locked_no_relay = True
            self.branch_var.set(Branch.NO_RELAY.value)
            self.branch_combo.configure(state="disabled")
            self._on_branch_change()
            self._update_sweep_preview()
            self._append_log("[Реле] Плата реле не найдена — полярность заблокирована на "
                             "\"no_relay\" (без коммутации, одна полярность).\n")
        elif relay_found and self._branch_locked_no_relay:
            self._branch_locked_no_relay = False
            self.branch_combo.configure(state="readonly")
            self._append_log("[Реле] Плата реле обнаружена — снова доступны "
                             "positive/negative/both.\n")

        # Живой счётчик "ресурсов видно" в статус-строке NI-VISA (баг-репорт:
        # раньше строка застывала на значении со старта программы). Трогаем
        # её только когда предполётная проверка прошла и сейчас не идёт
        # измерение — иначе затёрли бы "Измерение завершено"/"Ошибка" и т.п.
        if self._preflight_ok and getattr(self, '_visa_backend', None) and self.stop_btn.instate(['disabled']):
            n = state.resource_count if state.resource_count is not None else "?"
            live_status = f"NI-VISA: OK ({self._visa_backend}); ресурсов видно: {n}{self._visa_suffix}"
            self._set_status(live_status, "ok")

    def _rescan_discovery_now(self):
        """Кнопка «Обновить список» — форсирует один скан вне очереди опроса."""
        threading.Thread(target=self.discovery.rescan_now, daemon=True).start()

    def _do_blink(self, which: str):
        """
        «blink» (BETA, п.11): отправляет identify_command выбранному прибору,
        если он у него в конфиге настроен (у большинства сейчас — нет, см.
        instruments.identify_instrument — не сочиняем непроверенные SCPI-
        команды). Ищет конфиг по совпадению адреса с последним снимком
        обнаружения, а не переоткрывает *IDN? заново.

        Баг-репорт: спам по кнопке иногда ронял фоновое обнаружение
        (VI_ERROR_INV_OBJECT) — запрос гонялся за тот же VISA-ресурс
        параллельно с периодическим сканом DiscoveryService, без единой
        точки синхронизации. Фикс — тот же приём, что уже используется
        вокруг измерения и ручного режима (discovery.pause()/resume()),
        плюс блокировка самой кнопки на время запроса, чтобы повторный
        клик не породил вторую параллельную попытку.
        """
        btn = self._blink_btn_dmm if which == 'dmm' else self._blink_btn_src
        combo = self.e_dmm if which == 'dmm' else self.e_src
        addr = self._combo_address(combo)
        if not addr:
            messagebox.showinfo("blink", "Сначала выберите или введите адрес прибора.")
            return
        state = self.discovery.snapshot()
        match = next((i for i in state.instruments if i.address == addr), None)
        if match is None or match.config_path is None:
            messagebox.showinfo("blink", "Прибор по этому адресу пока не опознан сканом — "
                                         "нечем определить конфиг с командой мигания.")
            return

        import json as _json

        btn.configure(state="disabled")

        def worker():
            # pause() теперь СИНХРОННЫЙ (ждёт завершения фонового скана, см.
            # DiscoveryService.pause) — зовём его из рабочего потока, а не из
            # Tk-потока, чтобы короткое ожидание скана не подвешивало UI.
            self.discovery.pause()
            try:
                try:
                    from visa_backend import make_resource_manager
                    rm = make_resource_manager()
                    cfg = _json.loads(match.config_path.read_text(encoding='utf-8'))
                    ok = identify_instrument(rm, addr, cfg)
                    rm.close()
                except Exception as e:
                    self.events.put(("log", f"[blink] Ошибка: {e}\n"))
                    return
                if ok:
                    self.events.put(("log", f"[blink] Команда отправлена: {addr} ({match.config_path.stem})\n"))
                else:
                    self.events.put(("log", f"[blink] Для {match.config_path.stem} не настроена команда "
                                            "мигания (identify_command в конфиге отсутствует).\n"))
            finally:
                self.discovery.resume()
                self.events.put(("blink_done", which))

        threading.Thread(target=worker, daemon=True).start()

    # ---------------------------------------------------------- manual control
    def _manual_log(self, msg: str):
        self.events.put(("log", msg + "\n"))

    def _manual_open_session(self):
        if not self._preflight_ok:
            messagebox.showwarning("Проверка не пройдена",
                                   "Ручной режим недоступен: не пройдена предполётная проверка (NI-VISA/самотесты).")
            return
        excitation_type = self.excitation_var.get()
        v_limit = None
        i_limit = None
        if excitation_type == 'current':
            try:
                v_limit = float(self.e_vlimit.get().strip().replace(",", "."))
                if v_limit <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Проверьте параметры",
                                     "Для возбуждения током укажите положительное «Огр. напряжения» в панели параметров.")
                return
        else:
            try:
                i_limit = float(self.e_ilimit.get().strip().replace(",", "."))
                if i_limit <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Проверьте параметры",
                                     "Для возбуждения напряжением укажите положительное «Огр. тока» в панели параметров.")
                return

        addr = {
            "dmm_addr": self._combo_address(self.e_dmm) or None,
            "src_addr": self._combo_address(self.e_src) or None,
            "relay_port": self.e_relay.get().strip() or None,
        }
        self.manual_open_btn.configure(state="disabled")
        self.manual_status_label.configure(text="Открываю сессию…")

        def worker():
            from visa_backend import make_resource_manager
            from orchestrate import open_manual_control_session
            # pause() синхронный — из рабочего потока, чтобы не морозить UI.
            self.discovery.pause()
            try:
                rm = make_resource_manager()
                session = open_manual_control_session(
                    rm, excitation_type, V_limit=v_limit, I_limit=i_limit,
                    dmm_addr=addr["dmm_addr"], src_addr=addr["src_addr"], relay_port=addr["relay_port"],
                    log=self._manual_log, on_session_open=self._session.set,
                )
            except Exception as e:
                self.discovery.resume()
                self.events.put(("manual_error", str(e)))
                return
            self.events.put(("manual_opened", session))

        threading.Thread(target=worker, daemon=True).start()

    def _manual_close_session(self):
        with self._manual_lock:
            session = self._manual_session
            self._manual_session = None
        if session is None:
            return
        self.manual_close_btn.configure(state="disabled")

        def worker():
            try:
                session.stop()
            except Exception:
                pass
            session.close()
            self._session.clear()
            self.discovery.resume()
            self.events.put(("manual_closed", None))

        threading.Thread(target=worker, daemon=True).start()

    def _manual_set_relay(self, direction: str):
        with self._manual_lock:
            session = self._manual_session
        if session is None:
            return
        threading.Thread(target=session.set_relay, args=(direction,), daemon=True).start()

    def _manual_apply_setpoint(self):
        with self._manual_lock:
            session = self._manual_session
        if session is None:
            return
        try:
            value = float(self.e_manual_setpoint.get().strip().replace(",", "."))
        except ValueError:
            messagebox.showerror("Проверьте значение", "Уставка должна быть числом.")
            return

        # Жёсткий запрет платы реле (п.28) — та же проверка, что и для
        # обычной развёртки, здесь на одно число вместо диапазона. Ток
        # реально идёт через реле только при возбуждении током.
        if self.excitation_var.get() == 'current':
            block = relay_current_block_reason(abs(value))
            if block:
                messagebox.showerror("Недопустимая уставка", block)
                return
            if not self.suppress_warnings_var.get():
                warning = relay_current_warning(abs(value))
                if warning and not messagebox.askyesno("Подтверждение", f"⚠ {warning}\n\nПродолжить?"):
                    return

        threading.Thread(target=session.apply_setpoint, args=(value,), daemon=True).start()

    def _manual_stop(self):
        with self._manual_lock:
            session = self._manual_session
        if session is None:
            return
        threading.Thread(target=session.stop, daemon=True).start()

    # ---------------------------------------------------------- warning banner
    def _open_warning_banner(self):
        """
        п.16: почти на весь экран, мигающий восклицательный знак, большая
        кнопка «СТОП» прямо на баннере — иначе баннер перекрывает единственный
        способ остановить измерение, и режим безопасности превращается в
        свою противоположность. Только по ручной активации (эта кнопка).
        """
        if self._warning_banner is not None and self._warning_banner.winfo_exists():
            self._warning_banner.lift()
            return

        banner = tk.Toplevel(self.root)
        banner.title("ВНИМАНИЕ")
        banner.configure(bg="#ffffff")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        w, h = int(screen_w * 0.9), int(screen_h * 0.9)
        banner.geometry(f"{w}x{h}+{(screen_w - w) // 2}+{(screen_h - h) // 2}")
        try:
            banner.attributes("-topmost", True)
        except tk.TclError:
            pass

        mark = tk.Label(banner, text="⚠", font=("Segoe UI", 140, "bold"), fg="#dc2626", bg="#ffffff")
        mark.pack(pady=(30, 10))
        tk.Label(banner, text="ВНИМАНИЕ! Идут измерения!\nПриближаться к измерительному стенду ОПАСНО!",
                font=("Segoe UI Semibold", 26), fg="#dc2626", bg="#ffffff", justify="center").pack(pady=10)
        tk.Button(banner, text="■  СТОП", font=("Segoe UI Semibold", 32), bg="#dc2626", fg="white",
                 activebackground="#b91c1c", activeforeground="white",
                 command=self._request_stop, height=2, width=14).pack(pady=40)
        ttk.Button(banner, text="Закрыть баннер (измерение НЕ останавливает)",
                  command=lambda: self._close_warning_banner(banner)).pack(pady=(0, 10))
        banner.protocol("WM_DELETE_WINDOW", lambda: self._close_warning_banner(banner))

        self._warning_banner = banner
        self._warning_blink_on = True
        self._blink_warning_banner(mark)

    def _blink_warning_banner(self, mark):
        if self._warning_banner is None or not self._warning_banner.winfo_exists():
            return
        self._warning_blink_on = not self._warning_blink_on
        mark.configure(fg="#dc2626" if self._warning_blink_on else "#ffffff")
        self._warning_banner.after(500, lambda: self._blink_warning_banner(mark))

    def _close_warning_banner(self, banner):
        banner.destroy()
        self._warning_banner = None

    # -------------------------------------------------------------- work dir
    def _open_folder(self, path):
        """Открывает папку в проводнике; создаёт её, если ещё не существует (см. меню «Открыть папку»)."""
        path = Path(path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except Exception as e:
            messagebox.showerror("Не удалось открыть папку", f"{path}\n\n{e}")

    def _open_work_dir_dialog(self):
        """п.23: рабочая папка настраивается из UI, персистентно (apppaths.work_dir)."""
        from tkinter import filedialog
        chosen = filedialog.askdirectory(initialdir=str(self.data_dir), title="Выбрать рабочую папку")
        if not chosen:
            return
        new_dir = Path(chosen)
        set_work_dir(new_dir)
        self.data_dir = new_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_mgr = ConfigManager(self.data_dir / "ivtrace_config.json")
        self._append_log(f"Рабочая папка изменена: {new_dir}\n")
        messagebox.showinfo(
            "Рабочая папка",
            f"Рабочая папка установлена: {new_dir}\n\n"
            "Действует для новых измерений и построения графика по умолчанию.",
        )

    def _clear_cache_dialog(self):
        """
        Очистка накопленных результатов (CSV/PNG/XLSX в рабочей папке +
        Cache) и файла параметров последнего запуска — см.
        apppaths.clear_results_cache(). Конфиги приборов и профили
        датчиков в других директориях, эта функция их не видит вовсе.
        Подтверждение с точным числом файлов — действие безвозвратное.
        """
        from apppaths import work_dir as _work_dir_fn
        base = _work_dir_fn()
        candidates = []
        if base.is_dir():
            for pattern in ("IVtrace_*.csv", "IVtrace_*.png", "IVtrace_*.xlsx"):
                candidates.extend(base.glob(pattern))
            if (base / "ivtrace_config.json").exists():
                candidates.append(base / "ivtrace_config.json")
        cache = cache_dir()
        cache_files = [p for p in cache.rglob("*") if p.is_file()] if cache.is_dir() else []
        total = len(candidates) + len(cache_files)
        if total == 0:
            messagebox.showinfo("Очистить кэш", "Нечего чистить — рабочая папка и кэш уже пусты.")
            return
        if not messagebox.askyesno(
            "Очистить кэш",
            f"Будет удалено файлов: {total} (CSV/PNG/XLSX результатов, файл последних "
            "параметров, содержимое папки Cache).\n\n"
            "Конфиги приборов и профили датчиков не затрагиваются.\n\n"
            "Продолжить?",
        ):
            return
        removed = clear_results_cache()
        self._append_log(f"[Кэш] Удалено файлов: {len(removed)}\n")
        messagebox.showinfo("Очистить кэш", f"Удалено файлов: {len(removed)}.")

    # -------------------------------------------------------- calibration editor
    def _open_calibration_editor(self):
        """
        п.3-UI + бага 6/7: редактор поверки ФИЗИЧЕСКИХ приборов (реестр,
        см. calibration.py), а не конфигов моделей. Одна модель может дать
        несколько строк (по одному экземпляру, различённых серийным
        номером) — обе проблемы бага 6 (один прибор, два конфига
        current/voltage — теперь один model_id, одна строка) и бага 7
        (два физических прибора одной модели — теперь два S/N, две строки)
        решены на уровне данных, а не UI-костылём.

        Список моделей собирается из всех каталогов конфигов (мультиметр в
        обеих ролях + оба типа источника), а не только тех, что реально
        сейчас подключены — оператор может готовить записи заранее.
        """
        win = tk.Toplevel(self.root)
        win.title("Поверка приборов")
        win.geometry("820x480")
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        columns = ("model", "serial", "status", "date", "interval", "comment")
        tree = ttk.Treeview(win, columns=columns, show="headings", selectmode="browse")
        for col, text, width in (
            ("model", "Модель", 190), ("serial", "S/N", 90), ("status", "Статус", 90),
            ("date", "Поверка", 100), ("interval", "Интервал, мес.", 100), ("comment", "Комментарий", 140),
        ):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="w")
        tree.grid(row=0, column=0, columnspan=4, sticky="nsew", padx=10, pady=(10, 6))
        scroll = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=4, sticky="ns", pady=(10, 6))

        config_dirs = [multimeter_cfg_dir(), voltmeter_cfg_dir(), current_source_cfg_dir(), voltage_source_cfg_dir()]
        rows_by_iid = {}
        model_name_by_id = {}
        model_id_by_name = {}

        def refresh():
            for item in tree.get_children():
                tree.delete(item)
            rows_by_iid.clear()
            model_name_by_id.clear()
            model_id_by_name.clear()
            model_name_by_id.update(known_models(config_dirs))
            for model_id, model_name in model_name_by_id.items():
                model_id_by_name[model_name] = model_id
            for row in list_calibration_rows(config_dirs):
                iid = f"{row['model_id']}::{row['serial_number']}"
                rows_by_iid[iid] = row
                info = row['info']
                tree.insert("", "end", iid=iid, values=(
                    row['model_name'], row['serial_number'], info.status.value,
                    info.last_date.isoformat() if info.last_date else '',
                    row['calibration_interval_months'] or '',
                    row['comment'],
                ))

        refresh()

        form = ttk.Labelframe(win, text="Прибор", padding=8)
        form.grid(row=1, column=0, columnspan=5, sticky="ew", padx=10, pady=(0, 10))
        for c in range(6):
            form.columnconfigure(c, weight=1 if c in (1, 5) else 0)

        ttk.Label(form, text="Модель").grid(row=0, column=0, sticky="w")
        cb_model = ttk.Combobox(form, state="readonly", width=28)
        cb_model.grid(row=0, column=1, sticky="ew", padx=(4, 14))
        ttk.Label(form, text="S/N (пусто — единственный экземпляр)").grid(row=0, column=2, sticky="w")
        e_serial = ttk.Entry(form, width=14)
        e_serial.grid(row=0, column=3, padx=(4, 14))

        ttk.Label(form, text="Дата поверки (ГГГГ-ММ-ДД)").grid(row=1, column=0, sticky="w", pady=(6, 0))
        e_date = ttk.Entry(form, width=14)
        e_date.grid(row=1, column=1, sticky="w", padx=(4, 14), pady=(6, 0))
        ttk.Label(form, text="Интервал, мес.").grid(row=1, column=2, sticky="w", pady=(6, 0))
        e_interval = ttk.Entry(form, width=8)
        e_interval.grid(row=1, column=3, sticky="w", padx=(4, 14), pady=(6, 0))

        ttk.Label(form, text="Комментарий").grid(row=2, column=0, sticky="w", pady=(6, 0))
        e_comment = ttk.Entry(form)
        e_comment.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(4, 14), pady=(6, 0))

        def _refresh_model_combo():
            names = sorted(model_name_by_id.values())
            cb_model.configure(values=names)

        _refresh_model_combo()

        def on_select(_event=None):
            sel = tree.selection()
            if not sel:
                return
            row = rows_by_iid.get(sel[0])
            if row is None:
                return
            cb_model.set(row['model_name'])
            e_serial.delete(0, 'end'); e_serial.insert(0, row['serial_number'])
            e_comment.delete(0, 'end'); e_comment.insert(0, row['comment'])
            info = row['info']
            e_date.delete(0, 'end')
            if info.last_date:
                e_date.insert(0, info.last_date.isoformat())
            e_interval.delete(0, 'end')
            if row['calibration_interval_months']:
                e_interval.insert(0, str(row['calibration_interval_months']))

        tree.bind("<<TreeviewSelect>>", on_select)

        def save():
            model_name = cb_model.get().strip()
            model_id = model_id_by_name.get(model_name)
            if model_id is None:
                messagebox.showinfo("Поверка приборов", "Выберите модель из списка.", parent=win)
                return
            try:
                interval = int(e_interval.get().strip())
            except ValueError:
                messagebox.showerror("Ошибка", "Интервал должен быть целым числом месяцев.", parent=win)
                return
            try:
                set_calibration_record(
                    model_id, e_serial.get().strip(), e_date.get().strip(), interval,
                    comment=e_comment.get().strip(),
                )
            except ValueError as e:
                messagebox.showerror("Ошибка", str(e), parent=win)
                return
            refresh()
            self._append_log(f"Поверка обновлена: {model_name} (S/N: {e_serial.get().strip() or '—'})\n")

        def delete_selected():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Поверка приборов", "Выберите запись в списке.", parent=win)
                return
            row = rows_by_iid.get(sel[0])
            if row is None or not row['has_record']:
                messagebox.showinfo("Поверка приборов", "Эта строка — незаведённая модель, удалять нечего.", parent=win)
                return
            if not messagebox.askyesno("Поверка приборов",
                                       f"Удалить запись поверки: {row['model_name']} "
                                       f"(S/N: {row['serial_number'] or '—'})?", parent=win):
                return
            delete_calibration_record(row['model_id'], row['serial_number'])
            refresh()
            self._append_log(f"Запись поверки удалена: {row['model_name']} (S/N: {row['serial_number'] or '—'})\n")

        btns = ttk.Frame(form)
        btns.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Button(btns, text="Сохранить", command=save).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Удалить запись", command=delete_selected).pack(side="left")

    # -------------------------------------------------------------- sensor config
    def _save_config(self):
        name = self.e_config_name.get().strip()
        if not name:
            messagebox.showwarning("Имя конфига", "Введите имя для сохранения конфига.")
            return
        params = self._gather_params()
        if params is None:
            return
        # Сохраняем также I_nom, ratio и смещение нуля, если они есть
        try:
            inom = float(self.e_inom.get().strip().replace(",", ".")) if self.e_inom.get().strip() else None
            ratio = float(self.e_ratio.get().strip().replace(",", ".")) if self.e_ratio.get().strip() else None
            zero_offset = float(self.e_zero_offset.get().strip().replace(",", ".")) if self.e_zero_offset.get().strip() else None
        except ValueError:
            messagebox.showerror("Ошибка", "I ном., коэффициент и смещение нуля должны быть числами.")
            return
        params['I_nom'] = inom
        params['ratio'] = ratio
        params['zero_offset'] = zero_offset
        # Добавляем дополнительные опции
        params['stop_on_error'] = self.stop_on_error_var.get()
        try:
            params['error_threshold'] = float(self.e_error_threshold.get().strip().replace(",", "."))
        except ValueError:
            messagebox.showerror("Ошибка", "Порог погрешности должен быть числом.")
            return
        # branch/preset/turns/averaging уже в params — их положил _gather_params()

        try:
            path = self.sensor_config_mgr.save_sensor_config(name, params)
        except ValueError as e:
            messagebox.showerror("Недопустимое имя", str(e))
            return
        self._refresh_profile_list()
        self._append_log(f"Конфиг датчика сохранён: {path}\n")
        messagebox.showinfo("Успех", f"Конфиг сохранён как '{name}'.")

    def _rename_config(self):
        old_name = self.e_config_name.get().strip()
        if not old_name:
            messagebox.showwarning("Профиль", "Выберите профиль для переименования.")
            return
        new_name = simpledialog.askstring("Переименовать профиль", f"Новое имя для '{old_name}':",
                                          parent=self.root)
        if not new_name:
            return
        try:
            ok = self.sensor_config_mgr.rename_sensor_config(
                old_name, new_name, excitation_type=self.excitation_var.get())
        except ValueError as e:
            messagebox.showerror("Недопустимое имя", str(e))
            return
        if not ok:
            messagebox.showerror("Ошибка", f"Профиль '{old_name}' не найден.")
            return
        self._refresh_profile_list()
        self.e_config_name.set(new_name)
        self._append_log(f"Профиль датчика переименован: {old_name} -> {new_name}\n")

    def _delete_config(self):
        name = self.e_config_name.get().strip()
        if not name:
            messagebox.showwarning("Профиль", "Выберите профиль для удаления.")
            return
        if not messagebox.askyesno("Удалить профиль", f"Удалить профиль '{name}'? Это необратимо."):
            return
        ok = self.sensor_config_mgr.delete_sensor_config(name, excitation_type=self.excitation_var.get())
        if not ok:
            messagebox.showerror("Ошибка", f"Профиль '{name}' не найден.")
            return
        self._refresh_profile_list()
        self.e_config_name.set('')
        self._append_log(f"Профиль датчика удалён: {name}\n")

    def _load_config(self):
        name = self.e_config_name.get().strip()
        if not name:
            messagebox.showwarning("Имя конфига", "Введите имя конфига для загрузки.")
            return
        # Ищем именно в подпапке текущего выбранного типа возбуждения —
        # профиль датчика напряжения физически неприменим к промеру током
        # (см. config.SensorConfigManager, п.39).
        params = self.sensor_config_mgr.load_sensor_config(name, excitation_type=self.excitation_var.get())
        if params is None:
            messagebox.showerror("Ошибка", f"Конфиг '{name}' не найден или повреждён.")
            return

        # Заполняем поля интерфейса. Все числовые поля — через _set_entry:
        # сохранённый пустым параметр приходит как None, и раньше в поле попадал
        # литерал "None" (str(None)), из-за чего старт измерения падал на
        # float("None") (баг-репорт п.3 для смещения нуля; A2 — то же для
        # I ном./коэффициента). _set_entry превращает None в пустую строку.
        self.excitation_var.set(params.get('excitation_type', 'current'))
        self.output_var.set(params.get('output_type', 'current'))
        self._set_entry(self.e_start, params.get('X_start'))
        self._set_entry(self.e_stop, params.get('X_stop'))
        self._set_entry(self.e_step, params.get('X_step'))
        self._set_entry(self.e_vlimit, params.get('V_limit'))
        self._set_entry(self.e_ilimit, params.get('I_limit'))
        self._set_entry(self.e_delay, params.get('delay'))
        self._set_entry(self.e_cool, params.get('cooling_delay'))
        self._set_entry(self.e_label, params.get('label', ''))
        self._set_entry(self.e_inom, params.get('I_nom'))
        self._set_entry(self.e_ratio, params.get('ratio'))
        self._set_entry(self.e_zero_offset, params.get('zero_offset'))
        self._set_entry(self.e_turns, params.get('turns', 1.0))
        self.stop_on_error_var.set(params.get('stop_on_error', False))
        self._set_entry(self.e_error_threshold, params.get('error_threshold', 1.0))
        self._set_entry(self.e_recheck_count, params.get('recheck_count', MAX_MEASUREMENT_ATTEMPTS - 1))
        self.restore_local_var.set(params.get('restore_local', True))
        self.zero_crossing_smooth_var.set(params.get('zero_crossing_smooth', False))
        self._set_branch_safe(params.get('branch', Branch.BOTH.value))
        self._set_preset_safe(params.get('preset', DirectionPreset.DIVERGING.value))
        self._set_entry(self.e_avg_count, params.get('averaging_count', DEFAULT_AVERAGING_COUNT))
        self._set_entry(self.e_avg_delay, params.get('averaging_delay', DEFAULT_AVERAGING_DELAY))
        self.discard_first_var.set(params.get('discard_first', DEFAULT_DISCARD_FIRST))
        self.adaptive_cooling_var.set(params.get('adaptive_cooling', False))
        self._set_entry(self.e_cool_min, params.get('adaptive_cooling_min_delay', DEFAULT_ADAPTIVE_COOLING_MIN_DELAY))
        self._set_entry(self.e_cool_max, params.get('adaptive_cooling_max_delay', DEFAULT_ADAPTIVE_COOLING_MAX_DELAY))
        self.smooth_ramp_var.set(params.get('smooth_ramp', False))
        self._set_entry(self.e_ramp_duration, params.get('ramp_duration', 1.0))
        self.custom_program_var.set(bool(params.get('custom_program')))
        self.e_custom_program.delete("1.0", "end")
        if params.get('custom_program'):
            self.e_custom_program.insert("1.0", params['custom_program'])

        self._on_excitation_change()
        self._on_custom_program_change()
        self._on_adaptive_cooling_change()
        self._on_avg_count_change()
        self._on_branch_change()
        self._on_preset_change()
        self._update_sweep_preview()
        self._append_log(f"Конфиг датчика загружен: {name}\n")
        messagebox.showinfo("Успех", f"Конфиг '{name}' загружен.")

    @staticmethod
    def _set_entry(entry, value):
        """
        Заполняет Entry значением, превращая None (и строку 'None') в пустую
        строку (баг-репорт п.3/A2): профиль, сохранённый с пустым числовым
        полем, хранит там None; раньше str(None) клал в поле литерал 'None', и
        старт измерения падал на float('None'). Пустое поле трактуется дальше
        как «значение не задано» (optional_num -> None -> дефолт).
        """
        entry.delete(0, 'end')
        if value is not None and str(value) != 'None':
            entry.insert(0, str(value))

    # -------------------------------------------------------------- measurement
    def _gather_params(self):
        excitation_type = self.excitation_var.get()

        def num(entry, name):
            raw = entry.get().strip().replace(",", ".")
            if raw == "":
                raise ValueError(f"Поле «{name}» не заполнено.")
            return float(raw)

        def optional_num(entry):
            raw = entry.get().strip().replace(",", ".")
            if raw == "":
                return None
            return float(raw)

        # Плавное нарастание (BETA) прячет поля delay/cool (см.
        # _on_smooth_ramp_change) — с ними скрыт и смысл требовать их
        # заполненными; вместо этого требуется время шага.
        smooth_ramp = excitation_type == "current" and self.smooth_ramp_var.get()
        # Свой сценарий (BETA) прячет X_start/X_stop/X_step (см.
        # _on_custom_program_change) — вместо них требуется текст программы.
        custom_program_active = self.custom_program_var.get()

        try:
            params = {
                "excitation_type": excitation_type,
                "output_type": self.output_var.get(),
                "label": self.e_label.get().strip(),
            }
            if custom_program_active:
                custom_text = self.e_custom_program.get("1.0", "end").strip()
                if not custom_text:
                    raise ValueError("Поле «Свой сценарий» не заполнено.")
                try:
                    from sweep import parse_custom_program
                    parse_custom_program(custom_text)
                except ValueError as e:
                    raise ValueError(f"Свой сценарий: {e}")
                params["custom_program"] = custom_text
                params["X_start"] = None
                params["X_stop"] = None
                params["X_step"] = None
            else:
                params["custom_program"] = None
                params["X_start"] = num(self.e_start, "Начало")
                params["X_stop"] = num(self.e_stop, "Конец")
                params["X_step"] = num(self.e_step, "Шаг")
            if smooth_ramp:
                params["delay"] = 0.0
                params["cooling_delay"] = 0.0
                params["ramp_duration"] = num(self.e_ramp_duration, "Время шага")
            else:
                params["delay"] = num(self.e_delay, "Задержка установки")
                # e_cool скрыт при адаптивном охлаждении (см.
                # _on_adaptive_cooling_change) — сама cooling_delay в этом
                # режиме не используется (эффективную задержку считает
                # _adaptive_cooling_delay из min/max), требовать заполнения
                # скрытого поля не нужно.
                params["cooling_delay"] = (
                    optional_num(self.e_cool) or 0.0
                    if self.adaptive_cooling_var.get()
                    else num(self.e_cool, "Задержка охлаждения")
                )
                params["ramp_duration"] = optional_num(self.e_ramp_duration) or 1.0
            params["smooth_ramp"] = smooth_ramp
            if excitation_type == "current":
                params["V_limit"] = num(self.e_vlimit, "Огр. напряжения")
                params["I_limit"] = 0.0
            else:
                params["V_limit"] = 0.0
                params["I_limit"] = num(self.e_ilimit, "Огр. тока")

            # Новые параметры
            params["I_nom"] = optional_num(self.e_inom)
            params["ratio"] = optional_num(self.e_ratio)
            params["zero_offset"] = optional_num(self.e_zero_offset) or 0.0
            params["turns"] = optional_num(self.e_turns) or 1.0
            params["stop_on_error"] = self.stop_on_error_var.get()
            params["error_threshold"] = optional_num(self.e_error_threshold) or 1.0
            # Число доп. перепромеров при подозрении на брак (п.12): 0 = без
            # перепромеров. Отрицательное недопустимо.
            recheck = optional_num(self.e_recheck_count)
            recheck = MAX_MEASUREMENT_ATTEMPTS - 1 if recheck is None else int(recheck)
            if recheck < 0:
                raise ValueError("Число перепромеров не может быть отрицательным.")
            params["recheck_count"] = recheck
            params["restore_local"] = self.restore_local_var.get()
            params["zero_crossing_smooth"] = self.zero_crossing_smooth_var.get()
            params["branch"] = self.branch_var.get()
            params["preset"] = self.preset_var.get()
            params["averaging_count"] = int(optional_num(self.e_avg_count) or DEFAULT_AVERAGING_COUNT)
            params["averaging_delay"] = optional_num(self.e_avg_delay) or 0.0
            params["discard_first"] = self.discard_first_var.get()
            params["adaptive_cooling"] = self.adaptive_cooling_var.get()
            if params["adaptive_cooling"]:
                params["adaptive_cooling_min_delay"] = num(self.e_cool_min, "Мин. задержка охлаждения")
                params["adaptive_cooling_max_delay"] = num(self.e_cool_max, "Макс. задержка охлаждения")
                if params["adaptive_cooling_min_delay"] > params["adaptive_cooling_max_delay"]:
                    raise ValueError(
                        "Минимальная задержка охлаждения не может быть больше максимальной."
                    )
            else:
                params["adaptive_cooling_min_delay"] = optional_num(self.e_cool_min) or DEFAULT_ADAPTIVE_COOLING_MIN_DELAY
                params["adaptive_cooling_max_delay"] = optional_num(self.e_cool_max) or DEFAULT_ADAPTIVE_COOLING_MAX_DELAY
            params["suppress_notifications"] = self.suppress_warnings_var.get()

            # Без коэффициента преобразования нечем считать ожидаемый выход
            # датчика, поэтому отсечка по погрешности без него работать не
            # может. I_nom нужен наравне с ratio (баг-репорт): без номинала
            # отсечка живьём считала бы обычную относительную погрешность
            # вместо приведённой, которую показывает итоговый отчёт/график —
            # они расходились, особенно на малых уставках.
            if params["stop_on_error"] and params["ratio"] is None:
                raise ValueError("Для отсечки по погрешности необходимо указать коэффициент преобразования.")
            if params["stop_on_error"] and params["I_nom"] is None:
                raise ValueError("Для отсечки по погрешности необходимо указать номинальный первичный ток/напряжение.")

        except ValueError as e:
            messagebox.showerror("Проверьте параметры", str(e))
            return None

        errors = validate_measure_params(params, excitation_type)
        if errors:
            messagebox.showerror("Некорректные параметры", "\n".join(errors))
            return None
        return params

    def _start_measurement(self):
        if not self._preflight_ok:
            messagebox.showwarning("Проверка не пройдена",
                                   "Измерение недоступно: не пройдена предполётная проверка (NI-VISA/самотесты).")
            return
        params = self._gather_params()
        if params is None:
            return

        self.config_mgr.save(params)
        csv_path = make_csv_filename(self.data_dir, params["label"])

        if params.get("custom_program"):
            confirm_text = (
                f"Возбуждение: {params['excitation_type']}\n"
                f"Свой сценарий (BETA): {params['custom_program']}\n"
                "\nЗапустить измерение?"
            )
        else:
            branch_text = {
                Branch.BOTH.value: "Обе полярности через реле",
                Branch.POSITIVE.value: "Только положительная полярность",
                Branch.NEGATIVE.value: "Только отрицательная полярность",
                Branch.NO_RELAY.value: "Без реле (одна полярность, коммутация не используется)",
            }.get(params.get("branch", Branch.BOTH.value), "Обе полярности через реле")
            confirm_text = (
                f"Возбуждение: {params['excitation_type']}\n"
                f"Диапазон: {params['X_start']}..{params['X_stop']} (шаг {params['X_step']})\n"
                f"{branch_text}.\n\nЗапустить измерение?"
            )
        # Жёсткий запрет (>800 А) сюда не дойдёт вообще: он уже отсеян
        # в _gather_params -> validate_measure_params messagebox'ом с
        # ошибкой. Здесь только предупреждение о работе свыше паспортных
        # 400 А — оператор должен увидеть его непосредственно перед тем, как
        # подтверждает запуск, а не потом в логе.
        if not params["suppress_notifications"] and params.get("branch") != Branch.NO_RELAY.value:
            warning = relay_current_warning(current_sweep_max_abs(params, params["excitation_type"]))
            if warning:
                confirm_text = f"⚠ {warning}\n\n{confirm_text}"

        # Плавное нарастание на больших токах — предупреждение (баг-репорт:
        # раньше запрет), ампераж не ограничен; показываем перед стартом.
        if (not params["suppress_notifications"] and params.get("smooth_ramp")
                and params["excitation_type"] == "current"):
            ramp_warn = smooth_ramp_warning(current_sweep_max_abs(params, params["excitation_type"]))
            if ramp_warn:
                confirm_text = f"⚠ {ramp_warn}\n\n{confirm_text}"

        if not messagebox.askyesno("Запуск измерения", confirm_text):
            return

        self.stop_event.clear()
        self._set_running(True)
        self._append_log(f"\n=== Измерение: {csv_path.name} ===\n")
        # Нужны на "done" для автопостроения графика (п.22) — там уже нет
        # доступа к полям формы (оператор мог их поменять, пока шло измерение).
        self._last_measure_params = params

        addr = {
            "dmm_addr": self._combo_address(self.e_dmm) or None,
            "src_addr": self._combo_address(self.e_src) or None,
            "relay_port": self.e_relay.get().strip() or None,
        }
        # Служба обнаружения (п.25) не должна спорить с измерением за те же
        # VISA-ресурсы/serial-порт реле — pause() зовём В рабочем потоке
        # (он синхронный, ждёт фоновый скан; из Tk-потока подвесил бы UI),
        # возобновляется в _measure_worker.finally.
        self._start_countdown(params)
        self.worker = threading.Thread(target=self._measure_worker, args=(params, csv_path, addr), daemon=True)
        self.worker.start()

    # ------------------------------------------------------------------ countdown
    def _start_countdown(self, params):
        """
        п.15 (только GUI, см. PLAN_V2.md — CLI сознательно не трогаем, п.34).

        Оценка — по estimate_duration_seconds() на том же плане, что и
        реальный измерительный цикл (sweep.plan_sweep с теми же
        параметрами). Явно приблизительная (без VISA-задержек и повторов
        при превышении погрешности, см. докстринг estimate_duration_seconds)
        — подписана "≈", а не выдаётся за точный расчёт.
        """
        # Стартовое время и число точек плана — для ПЕРЕСЧЁТА оценки по факту
        # (баг-репорт п.7): estimate_duration_seconds не знает перепромеров при
        # браке, поэтому по мере прихода прогресса (_on_progress) остаток
        # экстраполируется от реально потраченного времени.
        self._measure_start_time = time.time()
        self._measure_total_points = 0
        try:
            if params.get('custom_program'):
                plan = plan_custom_sweep(params['custom_program'])
            else:
                plan = plan_sweep(
                    params['X_start'], params['X_stop'], params['X_step'],
                    branch=Branch(params.get('branch', Branch.BOTH.value)),
                    preset=DirectionPreset(params.get('preset', DirectionPreset.DIVERGING.value)),
                    zero_crossing_smooth=params.get('zero_crossing_smooth', False),
                )
            self._measure_total_points = len(plan)
            total = estimate_duration_seconds(
                plan, delay=params['delay'], cooling_delay=params['cooling_delay'],
                averaging_count=params.get('averaging_count', DEFAULT_AVERAGING_COUNT),
                averaging_delay=params.get('averaging_delay', DEFAULT_AVERAGING_DELAY),
                adaptive_cooling=params.get('adaptive_cooling', False),
                adaptive_cooling_min_delay=params.get('adaptive_cooling_min_delay', DEFAULT_ADAPTIVE_COOLING_MIN_DELAY),
                adaptive_cooling_max_delay=params.get('adaptive_cooling_max_delay', DEFAULT_ADAPTIVE_COOLING_MAX_DELAY),
                smooth_ramp=params.get('smooth_ramp', False),
                ramp_duration=params.get('ramp_duration') or 1.0,
            )
        except Exception:
            self.countdown_label.configure(text="")
            return

        self._countdown_remaining = int(round(total))
        self._refresh_countdown_finish_text()
        self._tick_countdown()

    def _refresh_countdown_finish_text(self):
        import datetime as _dt
        secs = max(0, self._countdown_remaining or 0)
        self._countdown_finish_text = (_dt.datetime.now() + _dt.timedelta(seconds=secs)).strftime("%H:%M:%S")

    def _on_progress(self, done, total):
        """
        Прогресс измерения (баг-репорт п.7): пересчитываем остаток по ФАКТУ —
        среднее реально потраченное время на снятую точку × число оставшихся.
        Так оценка учитывает перепромеры при браке и прочие задержки, которых
        теоретическая estimate_duration_seconds не знает.
        """
        if self._countdown_remaining is None or not done or not total:
            return
        elapsed = time.time() - getattr(self, '_measure_start_time', time.time())
        per_point = elapsed / done
        remaining = per_point * max(0, total - done)
        self._countdown_remaining = int(round(remaining))
        self._refresh_countdown_finish_text()
        # немедленно обновим подпись (не дожидаясь следующего тика)
        mm, ss = divmod(max(0, self._countdown_remaining), 60)
        self.countdown_label.configure(
            text=f"Снято {done}/{total}  ·  осталось ≈{mm:02d}:{ss:02d}  ·  окончание ≈{self._countdown_finish_text}")

    def _tick_countdown(self):
        if self._countdown_remaining is None:
            return
        mm, ss = divmod(max(0, self._countdown_remaining), 60)
        self.countdown_label.configure(
            text=f"Осталось (оценочно): ≈{mm:02d}:{ss:02d}  ·  окончание ≈{self._countdown_finish_text}")
        if self._countdown_remaining <= 0:
            return
        self._countdown_remaining -= 1
        self._countdown_after_id = self.root.after(1000, self._tick_countdown)

    def _stop_countdown(self):
        self._countdown_remaining = None
        if self._countdown_after_id is not None:
            try:
                self.root.after_cancel(self._countdown_after_id)
            except Exception:
                pass
            self._countdown_after_id = None
        self.countdown_label.configure(text="")

    def _measure_worker(self, params, csv_path, addr):
        from visa_backend import make_resource_manager
        from orchestrate import run_measurement_session

        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(self.events)
        rm = None
        try:
            # Синхронный pause() (ждёт фоновый скан) — здесь, в рабочем потоке,
            # ДО открытия VISA-ресурсов: гарантирует, что фоновое обнаружение
            # уже отпустило приборы и не столкнётся с discover_instruments.
            self.discovery.pause()
            rm = make_resource_manager()
            run_measurement_session(
                rm, params, csv_path,
                dmm_addr=addr["dmm_addr"], src_addr=addr["src_addr"], relay_port=addr["relay_port"],
                should_stop=self.stop_event.is_set,
                on_session_open=self._session.set,
                # Прогресс для пересчёта оценки времени по факту (баг-репорт п.7):
                # шлём (снято, всего) в очередь; главный поток пересчитывает остаток
                # (с учётом реально потраченного на перепромеры времени).
                on_point_done=lambda done, total: self.events.put(("progress", (done, total))),
            )
            self.events.put(("done", str(csv_path)))
        except Exception as e:
            # В файл-лог с трассировкой (applog) — чтобы разобрать сбой/вылет
            # постфактум на реальной машине; в UI — короткое сообщение.
            _log.exception("Ошибка измерительного потока")
            traceback.print_exc()
            self.events.put(("error", str(e)))
        finally:
            # Приборы этой сессии закрыты — ручка больше не должна вести на
            # мёртвые сессии, иначе следующий «Стоп» попытается писать в них.
            self._session.clear()
            self.discovery.resume()
            if rm is not None:
                try:
                    rm.close()
                except Exception:
                    pass
            sys.stdout, sys.stderr = old_out, old_err

    def _request_stop(self):
        """
        Кнопка «Стоп» = аварийное обесточивание, а не вежливая просьба.

        Раньше здесь выставлялся только флаг, и стенд оставался под током,
        пока измерительный цикл не доберётся до ближайшей проверки между
        точками — то есть до конца текущей точки со всеми её задержками.
        Оператор жмёт «Стоп», когда что-то идёт не так прямо сейчас, поэтому
        сначала гасим железо (из этого же потока, немедленно), и только
        потом просим цикл свернуться.
        """
        self.stop_btn.configure(state="disabled")
        self._append_log("\n… СТОП: обесточиваю стенд…\n")
        self._stop_countdown()

        handle = self._session.get()
        if handle is not None:
            for step in handle.emergency_stop():
                self._append_log(f"  {step}\n")
            self._append_log("Стенд обесточен.\n")

        # Флаг всё равно выставляем: если аварийная последовательность
        # почему-то не свалила цикл (например, приборы успели ответить),
        # он должен свернуться сам, а не идти дальше по точкам.
        self.stop_event.set()

    def _set_running(self, running):
        self.start_btn.configure(state="disabled" if running else ("normal" if self._preflight_ok else "disabled"))
        self.stop_btn.configure(state="normal" if running else "disabled")

    # ------------------------------------------------------------------ plot tab
    def _on_auto_range_change(self):
        if self.auto_range_var.get():
            self._range_fields_row.grid_remove()
        else:
            self._range_fields_row.grid()

    def _axis_range(self, lo_entry, hi_entry):
        """(min, max) из пары полей, либо None — если авто-диапазон включён
        или поля не заполнены/некорректны (тогда просто оставляем авто, не
        падая с ошибкой посреди построения графика)."""
        if self.auto_range_var.get():
            return None
        lo, hi = lo_entry.get().strip(), hi_entry.get().strip()
        if not lo or not hi:
            return None
        try:
            return (float(lo.replace(",", ".")), float(hi.replace(",", ".")))
        except ValueError:
            return None

    def _resolve_plot_csv_path(self):
        if self.plot_csv_path is not None:
            return self.plot_csv_path
        if self.last_csv:
            return Path(self.last_csv)
        from analysis import find_latest_csv
        try:
            return find_latest_csv(self.data_dir)
        except FileNotFoundError as e:
            messagebox.showerror("Нет данных", str(e))
            return None

    def _open_csv_for_plot(self):
        """п.20: вкладка «График» умеет открывать любой CSV, не только последний."""
        from tkinter import filedialog
        from analysis import metadata_i_nom_and_ratio

        current = self._resolve_plot_csv_path()
        initial_dir = str(current.parent) if current else str(self.data_dir)
        path = filedialog.askopenfilename(
            initialdir=initial_dir, filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")],
            title="Открыть CSV для анализа",
        )
        if not path:
            return
        path = Path(path)
        self.plot_csv_path = path
        self.plot_file_label.configure(text=path.name)

        # Предзаполняем I ном./коэффициент тем, с чем файл снимался, если это
        # сохранено в его собственной шапке — иначе оператор увидит в полях
        # значения от предыдущего файла, что не имеет смысла для нового.
        I_nom, X, excitation_type = metadata_i_nom_and_ratio(path)
        self.l_plot_inom.configure(text="U ном., В" if excitation_type == 'voltage' else "I ном., А")
        self.plot_e_inom.delete(0, "end")
        if I_nom is not None:
            self.plot_e_inom.insert(0, str(I_nom))
        self.plot_e_ratio.delete(0, "end")
        if X is not None:
            self.plot_e_ratio.insert(0, str(X))

        if I_nom is not None and X is not None:
            self._do_analyze()
        else:
            self._current_df = None
            self._refresh_points_tree()

    def _do_analyze(self):
        from analysis import load_and_analyze

        csv_path = self._resolve_plot_csv_path()
        if csv_path is None:
            return

        try:
            inom = float(self.plot_e_inom.get().strip().replace(",", "."))
            ratio = float(self.plot_e_ratio.get().strip().replace(",", "."))
            if inom <= 0 or ratio <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Проверьте параметры анализа",
                                 "I ном. и коэффициент X должны быть положительными числами.")
            return

        try:
            stats = load_and_analyze(
                Path(csv_path), I_nom=inom, X=ratio, save_png=True, show=False, close_fig=False,
                show_error_labels=self.show_labels_var.get(),
                xlim=self._axis_range(self.e_xmin, self.e_xmax),
                y1lim=self._axis_range(self.e_y1min, self.e_y1max),
                y2lim=self._axis_range(self.e_y2min, self.e_y2max),
            )
        except Exception as e:
            messagebox.showerror("Ошибка анализа", str(e))
            return

        self.plot_csv_path = Path(csv_path)
        self.plot_file_label.configure(text=self.plot_csv_path.name)
        self._current_df = stats["dataframe"]
        self._refresh_points_tree()

        excluded_note = f", исключено {stats['rejected_points']}/{stats['points']}" if stats['rejected_points'] else ""
        self._append_log(
            f"[Анализ] {self.plot_csv_path.name}: макс. погрешность {stats['max_error_percent']:+.4f} %, "
            f"средняя {stats['mean_error_percent']:+.4f} %{excluded_note}; PNG: {stats['png_path']}\n"
        )
        self._embed_figure(stats["figure"])
        self.notebook.select(2)

    def _do_estimate_ratio(self):
        """п.10 (BETA): фактический коэффициент по снятым точкам, отдельно от построения графика."""
        from analysis import estimate_ratio_from_data, metadata_zero_offset

        csv_path = self._resolve_plot_csv_path()
        if csv_path is None:
            return
        df = self._current_df if self._current_df is not None and self.plot_csv_path == csv_path \
            else pd.read_csv(csv_path, comment='#')

        # Баг-репорт: определение коэффициента НЕ учитывало смещение нуля.
        # Смещение вычитается из Y_meas ДО подгонки прямой через ноль (иначе
        # оно смещает саму оценку наклона). Берём то, что ввёл оператор в поле
        # смещения (актуальнее для «этого» датчика), а при пустом поле —
        # значение из шапки CSV, как раньше.
        raw_offset = self.e_zero_offset.get().strip().replace(",", ".")
        try:
            zero_offset = float(raw_offset) if raw_offset else (metadata_zero_offset(csv_path) or 0.0)
        except ValueError:
            messagebox.showerror("Смещение нуля", "Смещение нуля должно быть числом.")
            return

        try:
            result = estimate_ratio_from_data(df, zero_offset=zero_offset)
        except ValueError as e:
            messagebox.showerror("Не удалось определить коэффициент", str(e))
            return

        offset_note = f"\nУчтено смещение нуля: {zero_offset:g}" if zero_offset else "\nСмещение нуля: 0"
        messagebox.showinfo(
            "Коэффициент преобразования (BETA)",
            f"Фактический: 1:{result['X_actual']:.2f}\n"
            f"Округлённый (кратно 25): 1:{result['X_rounded']:.0f}\n"
            f"Расхождение: {result['discrepancy_percent']:.2f}%"
            f"{offset_note}",
        )

    # -------------------------------------------------------------- point editing
    def _refresh_points_tree(self):
        """п.26: таблица точек с возможностью исключить/вернуть без потери сырых данных."""
        for item in self.points_tree.get_children():
            self.points_tree.delete(item)
        df = self._current_df
        if df is None:
            return
        excitation_col = 'X_set' if 'X_set' in df.columns else 'I_set_A'
        # Y_meas — новая колонка выхода датчика (ось А-1, ток ИЛИ напряжение,
        # см. measurement.py); I_meas_A — старые CSV (до этого пункта плана),
        # где выход всегда трактовался как ток.
        if 'Y_meas' in df.columns:
            meas_col = 'Y_meas'
            meas_unit = df['Y_unit'].iloc[0] if 'Y_unit' in df.columns and len(df) else 'A'
        else:
            meas_col = 'I_meas_A'
            meas_unit = 'A'
        self.points_tree.heading('i_meas', text=f"Y изм., {meas_unit}")
        for idx, row in df.iterrows():
            rejected = bool(row['Rejected']) if 'Rejected' in df.columns and pd.notna(row.get('Rejected')) else False
            excluded = bool(row['ManuallyExcluded']) if 'ManuallyExcluded' in df.columns and pd.notna(row.get('ManuallyExcluded')) else False
            error = row.get('Error_percent')
            error_text = f"{error:+.4f}" if error is not None and pd.notna(error) else ""
            self.points_tree.insert("", "end", iid=str(idx), values=(
                row.get(excitation_col, ''), row.get(meas_col, ''), error_text,
                "да" if rejected else "", "да" if excluded else "",
            ))

    def _toggle_selected_points(self, excluded: bool):
        if self._current_df is None:
            messagebox.showinfo("Точки", "Сначала постройте график.")
            return
        selected = self.points_tree.selection()
        if not selected:
            return
        if 'ManuallyExcluded' not in self._current_df.columns:
            self._current_df['ManuallyExcluded'] = False
        for iid in selected:
            self._current_df.loc[int(iid), 'ManuallyExcluded'] = excluded
        self._refresh_points_tree()

    def _save_point_exclusions(self):
        """Пишет ManuallyExcluded обратно в CSV (данные не удаляются) и перестраивает график."""
        if self._current_df is None or self.plot_csv_path is None:
            messagebox.showinfo("Точки", "Сначала постройте график.")
            return
        # Баг-репорт: «Сохранить и перестроить» иногда молча ничего не делал.
        # Оборачиваем в try/except с логом и видимой ошибкой, чтобы сбой (нет
        # прав на запись, файл открыт в Excel и т.п.) не проглатывался.
        from analysis import save_dataframe_with_metadata
        try:
            n_excluded = 0
            if 'ManuallyExcluded' in self._current_df.columns:
                n_excluded = int(self._current_df['ManuallyExcluded'].fillna(False).astype(bool).sum())
            save_dataframe_with_metadata(self.plot_csv_path, self._current_df)
        except Exception as e:
            _log.exception("Сохранение исключённых точек не удалось")
            messagebox.showerror(
                "Не удалось сохранить",
                f"Не удалось записать изменения в файл:\n{e}\n\n"
                "Возможно, файл открыт в другой программе. Подробности — в журнале.")
            return
        self._append_log(f"Изменения точек сохранены ({n_excluded} исключено): {self.plot_csv_path}\n")
        # Перестраиваем график по обновлённому файлу; исключённые точки теперь
        # не строятся (см. analysis.load_and_analyze). _do_analyze сам покажет
        # ошибку и переключит на вкладку графика.
        self._do_analyze()

    def _do_invert_input(self):
        """
        п.26/invert_input: пост-обработка уже снятого файла, перенесённая из
        измерительного цикла. НЕ РЕКОМЕНДУЕТСЯ — пишет НОВЫЙ файл, исходные
        данные измерения не трогает.
        """
        csv_path = self._resolve_plot_csv_path()
        if csv_path is None:
            return
        if not messagebox.askyesno(
            "Инвертировать вход",
            "Инверсия знака возбуждения НЕ РЕКОМЕНДУЕТСЯ (см. документацию) — предназначена только "
            "для случая, когда датчик физически подключён в обратной полярности, а перекоммутировать "
            "его на стенде нельзя.\n\n"
            f"Будет создан НОВЫЙ файл рядом с «{csv_path.name}», исходные данные не изменятся.\n"
            "Продолжить?",
        ):
            return
        from analysis import apply_invert_input, metadata_i_nom_and_ratio
        output_path = apply_invert_input(csv_path)
        self._append_log(f"Инвертированная копия сохранена: {output_path}\n")
        if messagebox.askyesno("Готово", f"Файл создан: {output_path.name}\nОткрыть его для анализа?"):
            self.plot_csv_path = output_path
            self.plot_file_label.configure(text=output_path.name)
            I_nom, X, excitation_type = metadata_i_nom_and_ratio(output_path)
            self.l_plot_inom.configure(text="U ном., В" if excitation_type == 'voltage' else "I ном., А")
            self.plot_e_inom.delete(0, "end")
            if I_nom is not None:
                self.plot_e_inom.insert(0, str(I_nom))
            self.plot_e_ratio.delete(0, "end")
            if X is not None:
                self.plot_e_ratio.insert(0, str(X))
            if I_nom is not None and X is not None:
                self._do_analyze()

    # -------------------------------------------------------------------- export
    def _do_export_xlsx(self):
        from tkinter import filedialog
        from analysis import export_xlsx

        csv_path = self._resolve_plot_csv_path()
        if csv_path is None:
            return
        out = filedialog.asksaveasfilename(
            initialdir=str(csv_path.parent), initialfile=csv_path.with_suffix('.xlsx').name,
            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")], title="Экспорт в XLSX",
        )
        if not out:
            return
        try:
            path = export_xlsx(csv_path, xlsx_path=Path(out))
        except Exception as e:
            # export_xlsx уже записал трассировку в лог (applog). Здесь —
            # видимое оператору сообщение вместо прежнего тихого «ничего не
            # произошло» (баг-репорт: XLSX «не сохраняется» без объяснений).
            _log.exception("Экспорт XLSX не удался")
            messagebox.showerror(
                "Экспорт не удался",
                f"Не удалось сохранить XLSX:\n{e}\n\nПодробности — в журнале приложения.",
            )
            return
        self._append_log(f"XLSX сохранён: {path}\n")
        messagebox.showinfo("Экспорт", f"Файл сохранён:\n{path}")

    # -------------------------------------------------------- автопостроение (п.22)
    def _auto_plot_after_measurement(self, csv_path: Path):
        """
        По окончании измерения график сохраняется и открывается автоматически
        (п.22), не ломая ручной режим (п.20 — кнопка «Построить график» и
        открытие произвольного файла работают точно так же, как раньше).

        Без I ном./коэффициента в параметрах измерения строить не из чего —
        это не ошибка измерения, тихо пропускаем с пометкой в журнале.
        """
        from analysis import load_and_analyze_from_params

        params = self._last_measure_params or {}
        try:
            stats = load_and_analyze_from_params(
                csv_path, params, save_png=True, show=False, close_fig=False,
                show_error_labels=self.show_labels_var.get(),
            )
        except Exception as e:
            self._append_log(f"[График] Не удалось построить: {e}\n")
            return

        if stats is None:
            self._append_log("[График] Пропущен: не заданы I ном. и коэффициент преобразования.\n")
            return

        self.plot_csv_path = csv_path
        self.plot_file_label.configure(text=csv_path.name)
        self.plot_e_inom.delete(0, "end"); self.plot_e_inom.insert(0, str(params.get('I_nom')))
        self.plot_e_ratio.delete(0, "end"); self.plot_e_ratio.insert(0, str(params.get('ratio')))
        self._current_df = stats["dataframe"]
        self._refresh_points_tree()

        self._append_log(
            f"[График] Сохранён: {stats['png_path']} (макс. {stats['max_error_percent']:+.4f} %, "
            f"средняя {stats['mean_error_percent']:+.4f} %)\n"
        )
        self._embed_figure(stats["figure"])
        self.notebook.select(2)

    def _embed_figure(self, fig):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

        # Старый холст (Tk-виджеты) уничтожаем на ГЛАВНОМ потоке. Саму фигуру
        # (объектный Figure без pyplot-менеджера, см. analysis.load_and_analyze)
        # НЕ закрываем через plt.close — у неё нет Tcl-обработчика, она безопасно
        # соберётся сборщиком мусора; plt.close тут только тянул бы pyplot и его
        # потоко-небезопасный менеджер обратно (баг-репорт про вылеты).
        for w in self.plot_frame.winfo_children():
            w.destroy()
        self._current_fig = fig
        fig.set_size_inches(8.5, 6.2)

        canvas = FigureCanvasTkAgg(fig, master=self.plot_frame)
        canvas.draw()
        canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        toolbar_frame = ttk.Frame(self.plot_frame)
        toolbar_frame.grid(row=1, column=0, sticky="ew")
        NavigationToolbar2Tk(canvas, toolbar_frame).update()

    # -------------------------------------------------------------- event loop
    def _on_close(self):
        # Просим остановить измерение и аккуратно гасим цикл событий, чтобы
        # не ловить "invalid command name ..._drain_events" на уже
        # уничтоженном окне.
        self._closing = True
        # Закрытие окна во время измерения обесточивает стенд так же жёстко,
        # как кнопка «Стоп»: рабочий поток — daemon, он умрёт вместе с
        # процессом и никаких finally не выполнит, а стенд останется под
        # током с замкнутым реле.
        handle = self._session.get()
        if handle is not None:
            try:
                handle.emergency_stop()
            except Exception:
                pass
        # Открытая ручная сессия (п.13/40) — та же логика: закрытие окна не
        # должно оставить стенд под током только потому, что оператор не
        # нажал отдельную кнопку «Остановить» в ручном режиме.
        with self._manual_lock:
            manual = self._manual_session
        if manual is not None:
            try:
                manual.emergency_stop()
            except Exception:
                pass
        self.stop_event.set()
        try:
            self.discovery.stop()
        except Exception:
            pass
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
        if self._discovery_after_id is not None:
            try:
                self.root.after_cancel(self._discovery_after_id)
            except Exception:
                pass
        if self._countdown_after_id is not None:
            try:
                self.root.after_cancel(self._countdown_after_id)
            except Exception:
                pass
        if self._warning_banner is not None:
            try:
                self._warning_banner.destroy()
            except Exception:
                pass
        # Объектную Figure (без pyplot-менеджера) отдельно закрывать не нужно —
        # она соберётся сборщиком мусора; pyplot сюда не тянем (см. _embed_figure).
        self._current_fig = None
        self.root.destroy()

    def _drain_events(self):
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "preflight":
                    ok, status, detail, visa_backend, visa_suffix = payload
                    self._preflight_ok = ok
                    self._visa_backend = visa_backend
                    self._visa_suffix = visa_suffix
                    self._set_status(status, "ok" if ok else "error")
                    self.footer_label.configure(
                        text=("Готово к измерению." if ok
                              else "Измерение заблокировано. См. журнал / установите NI-VISA."))
                    self.start_btn.configure(state="normal" if ok else "disabled")
                elif kind == "progress":
                    done, total = payload
                    self._on_progress(done, total)
                elif kind == "done":
                    self.last_csv = payload
                    self._append_log(f"\n✔ Измерение завершено. Данные: {payload}\n")
                    self._set_running(False)
                    self._set_status("Измерение завершено", "ok")
                    self._stop_countdown()
                    self._auto_plot_after_measurement(Path(payload))
                elif kind == "error":
                    self._append_log(f"\n✖ Ошибка: {payload}\n")
                    self._set_running(False)
                    self._set_status("Ошибка измерения", "error")
                    self._stop_countdown()
                    messagebox.showerror("Ошибка измерения", payload)
                elif kind == "manual_opened":
                    with self._manual_lock:
                        self._manual_session = payload
                    self.manual_close_btn.configure(state="normal")
                    for btn in self.manual_relay_buttons:
                        btn.configure(state="normal")
                    self.e_manual_setpoint.configure(state="normal")
                    self.manual_apply_btn.configure(state="normal")
                    self.manual_stop_btn.configure(state="normal")
                    self.manual_status_label.configure(text="Сессия открыта.")
                elif kind == "manual_error":
                    self.manual_open_btn.configure(state="normal")
                    self.manual_status_label.configure(text="Сессия не открыта.")
                    self._append_log(f"\n✖ Не удалось открыть ручной режим: {payload}\n")
                    messagebox.showerror("Ошибка ручного режима", payload)
                elif kind == "blink_done":
                    btn = self._blink_btn_dmm if payload == 'dmm' else self._blink_btn_src
                    btn.configure(state="normal")
                elif kind == "manual_closed":
                    self.manual_open_btn.configure(state="normal")
                    self.manual_close_btn.configure(state="disabled")
                    for btn in self.manual_relay_buttons:
                        btn.configure(state="disabled")
                    self.e_manual_setpoint.configure(state="disabled")
                    self.manual_apply_btn.configure(state="disabled")
                    self.manual_stop_btn.configure(state="disabled")
                    self.manual_status_label.configure(text="Сессия закрыта.")
        except queue.Empty:
            pass
        if not self._closing:
            self._after_id = self.root.after(120, self._drain_events)


def launch_gui(args=None) -> int:
    """Точка входа GUI. Возвращает 0 после закрытия окна."""
    root = tk.Tk()
    IVTraceGUI(root, args)
    root.mainloop()
    return 0
