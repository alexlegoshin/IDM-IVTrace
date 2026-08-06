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
  - вкладка «График» строит и встраивает тот же график, что и CLI analyze.
"""
import io
import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import pandas as pd

from apppaths import default_data_dir, sensor_config_dir
from config import ConfigManager, SensorConfigManager
from cli import current_sweep_max_abs, make_csv_filename, validate_measure_params
from limits import relay_current_warning
from measurement import (
    EXCITATION_UNITS,
    DEFAULT_AVERAGING_COUNT, DEFAULT_AVERAGING_DELAY, DEFAULT_DISCARD_FIRST,
)
from sweep import Branch, DirectionPreset


ACCENT = "#2563eb"
ACCENT_ACTIVE = "#1d4ed8"
BG = "#f4f5f7"
CARD = "#ffffff"
OK_COLOR = "#15803d"
ERR_COLOR = "#b91c1c"
BUSY_COLOR = "#b45309"
MUTED = "#6b7280"


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
        self.data_dir = Path(getattr(args, "data_dir", None) or default_data_dir())
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

        self._closing = False
        self._after_id = None

        self._build_style()
        self._build_ui()
        self._prefill_from_config()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.root.after(120, self._drain_events)
        self._run_preflight()

    # ------------------------------------------------------------------ style
    def _build_style(self):
        self.root.title("IVTrace")
        self.root.geometry("1020x680")
        self.root.minsize(920, 600)
        self.root.configure(bg=BG)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        base_font = ("Segoe UI", 10)
        style.configure(".", font=base_font, background=BG)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG)
        style.configure("Card.TLabel", background=CARD)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, font=("Segoe UI Semibold", 17))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("TLabelframe", background=BG, bordercolor="#d1d5db")
        style.configure("TLabelframe.Label", background=BG, foreground="#374151",
                        font=("Segoe UI Semibold", 10))
        style.configure("TEntry", padding=4)
        style.configure("TButton", padding=(12, 6))
        style.configure("Accent.TButton", padding=(16, 8), foreground="white",
                        background=ACCENT, font=("Segoe UI Semibold", 10), borderwidth=0)
        style.map("Accent.TButton",
                  background=[("active", ACCENT_ACTIVE), ("disabled", "#9ca3af")])
        style.configure("Danger.TButton", padding=(14, 8))

    # --------------------------------------------------------------------- ui
    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # ---- header ----
        header = ttk.Frame(self.root, padding=(18, 14, 18, 6))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="IVTrace", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Амплитудная характеристика датчиков тока/напряжения",
                  style="Sub.TLabel").grid(row=1, column=0, sticky="w")

        self.status_dot = tk.Canvas(header, width=12, height=12, bg=BG, highlightthickness=0)
        self.status_dot.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 8))
        self._dot = self.status_dot.create_oval(2, 2, 10, 10, fill=MUTED, outline="")
        self.status_label = ttk.Label(header, text="Инициализация…", style="Muted.TLabel")
        self.status_label.grid(row=0, column=2, rowspan=2, sticky="e")

        # ---- body: two columns ----
        body = ttk.Frame(self.root, padding=(18, 6, 18, 6))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, minsize=340)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_params(body)
        self._build_right(body)

        # ---- footer / preflight bar ----
        footer = ttk.Frame(self.root, padding=(18, 6, 18, 12))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.footer_label = ttk.Label(footer, text="", style="Muted.TLabel")
        self.footer_label.grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Проверить снова", command=self._run_preflight).grid(row=0, column=1, sticky="e")

    def _build_params(self, parent):
        left = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)

        # --- excitation type ---
        exc = ttk.Labelframe(left, text="Тип возбуждения", padding=10)
        exc.grid(row=0, column=0, sticky="ew")
        ttk.Radiobutton(exc, text="Ток (источник тока)", value="current",
                        variable=self.excitation_var, command=self._on_excitation_change).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(exc, text="Напряжение (источник напряжения)", value="voltage",
                        variable=self.excitation_var, command=self._on_excitation_change).grid(row=1, column=0, sticky="w")

        # --- output type (ось А-1, независимая от возбуждения: чем датчик
        # возбуждают — не то же самое, что и то, что он выдаёт на выходе) ---
        ttk.Separator(exc, orient="horizontal").grid(row=2, column=0, sticky="ew", pady=(8, 6))
        out_row = ttk.Frame(exc)
        out_row.grid(row=3, column=0, sticky="w")
        ttk.Label(out_row, text="Выход датчика:").pack(side="left", padx=(0, 6))
        self.output_var = tk.StringVar(value="current")
        ttk.Combobox(
            out_row, textvariable=self.output_var, state="readonly", width=10,
            values=["current", "voltage"],
        ).pack(side="left")

        # --- numeric params ---
        pf = ttk.Labelframe(left, text="Параметры измерения", padding=10)
        pf.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        pf.columnconfigure(1, weight=1)

        self.e_start, self.u_start = self._param_row(pf, 0, "Начало")
        self.e_stop, self.u_stop = self._param_row(pf, 1, "Конец")
        self.e_step, self.u_step = self._param_row(pf, 2, "Шаг")
        self.e_vlimit, self.u_vlimit = self._param_row(pf, 3, "Огр. напряжения", unit="В")
        self.e_delay, self.u_delay = self._param_row(pf, 4, "Задержка установки", unit="с")
        self.e_cool, self.u_cool = self._param_row(pf, 5, "Задержка охлаждения", unit="с")

        ttk.Label(pf, text="I ном., А").grid(row=6, column=0, sticky="w", pady=3)
        self.e_inom = ttk.Entry(pf, width=12)
        self.e_inom.grid(row=6, column=1, sticky="ew", pady=3, padx=(8, 6))
        ttk.Label(pf, text="Коэфф. 1:X").grid(row=7, column=0, sticky="w", pady=3)
        self.e_ratio = ttk.Entry(pf, width=12)
        self.e_ratio.grid(row=7, column=1, sticky="ew", pady=3, padx=(8, 6))
        ttk.Label(pf, text="Витки").grid(row=8, column=0, sticky="w", pady=3)
        self.e_turns = ttk.Entry(pf, width=12)
        self.e_turns.grid(row=8, column=1, sticky="ew", pady=3, padx=(8, 6))
        self.e_turns.insert(0, "1")

        ttk.Label(pf, text="Комментарий").grid(row=9, column=0, sticky="w", pady=(6, 0))
        self.e_label = ttk.Entry(pf)
        self.e_label.grid(row=9, column=1, columnspan=2, sticky="ew", pady=(6, 0))

        # --- optional instrument addresses ---
        adv = ttk.Labelframe(left, text="Приборы (необязательно, иначе автопоиск)", padding=10)
        adv.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        adv.columnconfigure(1, weight=1)
        self.e_dmm = self._addr_row(adv, 0, "Мультиметр VISA")
        self.e_src = self._addr_row(adv, 1, "Источник VISA")
        self.e_relay = self._addr_row(adv, 2, "Порт реле (COMx)")

        # --- additional options ---
        opts = ttk.Labelframe(left, text="Дополнительные опции", padding=10)
        opts.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        opts.columnconfigure(1, weight=1)

        self.stop_on_error_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Остановить при превышении погрешности",
                        variable=self.stop_on_error_var).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(opts, text="Порог погрешности, %").grid(row=1, column=0, sticky="w", pady=3)
        self.e_error_threshold = ttk.Entry(opts, width=10)
        self.e_error_threshold.grid(row=1, column=1, sticky="w", pady=3, padx=(8, 0))
        self.e_error_threshold.insert(0, "1.0")

        ttk.Label(opts, text="Полярность").grid(row=2, column=0, sticky="w", pady=(6, 3))
        self.branch_var = tk.StringVar(value=Branch.BOTH.value)
        ttk.Combobox(
            opts, textvariable=self.branch_var, state="readonly", width=10,
            values=[b.value for b in Branch],
        ).grid(row=2, column=1, sticky="w", pady=(6, 3))

        ttk.Label(opts, text="Схема прохода").grid(row=3, column=0, sticky="w", pady=3)
        self.preset_var = tk.StringVar(value=DirectionPreset.DIVERGING.value)
        ttk.Combobox(
            opts, textvariable=self.preset_var, state="readonly", width=10,
            values=[p.value for p in DirectionPreset],
        ).grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(opts, text="(имеет значение только при «both»)",
                  foreground="gray").grid(row=4, column=0, columnspan=2, sticky="w")

        ttk.Label(opts, text="Отсчётов на усреднение").grid(row=5, column=0, sticky="w", pady=(6, 3))
        self.e_avg_count = ttk.Entry(opts, width=6)
        self.e_avg_count.grid(row=5, column=1, sticky="w", pady=(6, 3))
        self.e_avg_count.insert(0, str(DEFAULT_AVERAGING_COUNT))
        ttk.Label(opts, text="Задержка между ними, с").grid(row=6, column=0, sticky="w", pady=3)
        self.e_avg_delay = ttk.Entry(opts, width=6)
        self.e_avg_delay.grid(row=6, column=1, sticky="w", pady=3)
        self.e_avg_delay.insert(0, str(DEFAULT_AVERAGING_DELAY))
        self.discard_first_var = tk.BooleanVar(value=DEFAULT_DISCARD_FIRST)
        ttk.Checkbutton(opts, text="Отбрасывать первый отсчёт",
                        variable=self.discard_first_var).grid(row=7, column=0, columnspan=2, sticky="w")

        self.adaptive_cooling_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Адаптивное охлаждение (BETA, растёт с током)",
                        variable=self.adaptive_cooling_var).grid(row=8, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="не проверено на реальном стенде",
                  foreground="gray").grid(row=9, column=0, columnspan=2, sticky="w")

        # --- sensor configuration ---
        cfg = ttk.Labelframe(left, text="Конфигурация датчика", padding=10)
        cfg.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        cfg.columnconfigure(1, weight=1)
        ttk.Label(cfg, text="Имя конфига").grid(row=0, column=0, sticky="w", pady=3)
        self.e_config_name = ttk.Entry(cfg)
        self.e_config_name.grid(row=0, column=1, sticky="ew", pady=3, padx=(8, 0))
        btn_frame = ttk.Frame(cfg)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(btn_frame, text="Сохранить конфиг", command=self._save_config).pack(side="left", padx=(0, 6))
        ttk.Button(btn_frame, text="Загрузить конфиг", command=self._load_config).pack(side="left")

        # --- action buttons ---
        actions = ttk.Frame(left)
        actions.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(actions, text="▶  Старт измерения", style="Accent.TButton",
                                    command=self._start_measurement, state="disabled")
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_btn = ttk.Button(actions, text="■  Стоп", style="Danger.TButton",
                                   command=self._request_stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        ttk.Checkbutton(left, text="Игнорировать самотесты (не рекомендуется)",
                        variable=self.skip_selftest_var,
                        command=self._run_preflight).grid(row=6, column=0, sticky="w", pady=(8, 0))

        self._on_excitation_change()

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
        nb.add(plot_tab, text="  График  ")
        plot_tab.rowconfigure(5, weight=1)
        plot_tab.columnconfigure(0, weight=1)

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
        ttk.Label(an, text="I ном., А").grid(row=0, column=0, sticky="w")
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
        self.show_labels_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(disp, text="Подписи погрешности над точками",
                        variable=self.show_labels_var).grid(row=0, column=0, columnspan=6, sticky="w")

        self.auto_range_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(disp, text="Авто-диапазон осей", variable=self.auto_range_var,
                        command=self._on_auto_range_change).grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))
        ttk.Label(disp, text="X мин/макс").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.e_xmin = ttk.Entry(disp, width=8); self.e_xmin.grid(row=2, column=1, padx=(4, 4))
        self.e_xmax = ttk.Entry(disp, width=8); self.e_xmax.grid(row=2, column=2, padx=(0, 14))
        ttk.Label(disp, text="Y выход мин/макс").grid(row=2, column=3, sticky="w")
        self.e_y1min = ttk.Entry(disp, width=8); self.e_y1min.grid(row=2, column=4, padx=(4, 4))
        self.e_y1max = ttk.Entry(disp, width=8); self.e_y1max.grid(row=2, column=5, padx=(0, 14))
        ttk.Label(disp, text="Y погр.,% мин/макс").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.e_y2min = ttk.Entry(disp, width=8); self.e_y2min.grid(row=3, column=1, padx=(4, 4), pady=(4, 0))
        self.e_y2max = ttk.Entry(disp, width=8); self.e_y2max.grid(row=3, column=2, padx=(0, 14), pady=(4, 0))
        self._range_entries = (self.e_xmin, self.e_xmax, self.e_y1min, self.e_y1max, self.e_y2min, self.e_y2max)
        self._on_auto_range_change()

        # -- правка точек (п.26): исключение не удаляет данные, только помечает --
        pts = ttk.Labelframe(plot_tab, text="Точки", padding=8)
        pts.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        pts.columnconfigure(0, weight=1)
        columns = ("x", "i_meas", "error", "rejected", "excluded")
        self.points_tree = ttk.Treeview(pts, columns=columns, show="headings", height=5, selectmode="extended")
        for col, text, width in (
            ("x", "Возбуждение", 100), ("i_meas", "I изм., А", 90), ("error", "Погр., %", 90),
            ("rejected", "Брак (авто)", 90), ("excluded", "Исключена", 90),
        ):
            self.points_tree.heading(col, text=text)
            self.points_tree.column(col, width=width, anchor="center")
        self.points_tree.grid(row=0, column=0, columnspan=4, sticky="ew")
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

        self.plot_frame = ttk.Frame(plot_tab, style="Card.TFrame")
        self.plot_frame.grid(row=5, column=0, sticky="nsew")
        self.plot_frame.rowconfigure(0, weight=1)
        self.plot_frame.columnconfigure(0, weight=1)
        self.plot_hint = ttk.Label(self.plot_frame, style="Muted.TLabel",
                                   text="После измерения задайте I ном. и X, затем «Построить график».\n"
                                        "Либо откройте любой CSV кнопкой выше.")
        self.plot_hint.grid(row=0, column=0)

        self.notebook = nb

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
            "V_limit": self.e_vlimit, "delay": self.e_delay, "cooling_delay": self.e_cool,
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
        # Огр. напряжения актуально только для источника тока.
        self.e_vlimit.configure(state="normal" if is_current else "disabled")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_status(self, text, kind="muted"):
        color = {"ok": OK_COLOR, "error": ERR_COLOR, "busy": BUSY_COLOR}.get(kind, MUTED)
        self.status_label.configure(text=text, foreground=color)
        self.status_dot.itemconfigure(self._dot, fill=color)

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
                self.events.put(("preflight", (False, "NI-VISA не найдена", visa.message)))
                return

            if self.skip_selftest_var.get():
                self.events.put(("preflight", (True, visa.summary_line() + " · самотесты пропущены", visa.message)))
                return

            from selftest import run_selftests
            self.events.put(("log", "[Самотесты] запуск виртуальной проверки кода…\n"))
            st = run_selftests()
            self.events.put(("log", f"[Самотесты] {st.summary}\n"))
            status = visa.summary_line() + (" · самотесты OK" if st.ok else " · САМОТЕСТЫ ПРОВАЛЕНЫ")
            self.events.put(("preflight", (st.ok, status, st.output if not st.ok else visa.message)))
        except Exception as e:
            self.events.put(("preflight", (False, "Ошибка проверки", str(e))))

    # -------------------------------------------------------------- sensor config
    def _save_config(self):
        name = self.e_config_name.get().strip()
        if not name:
            messagebox.showwarning("Имя конфига", "Введите имя для сохранения конфига.")
            return
        params = self._gather_params()
        if params is None:
            return
        # Сохраняем также I_nom и ratio, если они есть
        try:
            inom = float(self.e_inom.get().strip().replace(",", ".")) if self.e_inom.get().strip() else None
            ratio = float(self.e_ratio.get().strip().replace(",", ".")) if self.e_ratio.get().strip() else None
        except ValueError:
            messagebox.showerror("Ошибка", "I ном. и коэффициент должны быть числами.")
            return
        params['I_nom'] = inom
        params['ratio'] = ratio
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
        self._append_log(f"Конфиг датчика сохранён: {path}\n")
        messagebox.showinfo("Успех", f"Конфиг сохранён как '{name}'.")

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

        # Заполняем поля интерфейса
        self.excitation_var.set(params.get('excitation_type', 'current'))
        self.output_var.set(params.get('output_type', 'current'))
        self.e_start.delete(0, 'end'); self.e_start.insert(0, str(params.get('X_start', '')))
        self.e_stop.delete(0, 'end'); self.e_stop.insert(0, str(params.get('X_stop', '')))
        self.e_step.delete(0, 'end'); self.e_step.insert(0, str(params.get('X_step', '')))
        self.e_vlimit.delete(0, 'end'); self.e_vlimit.insert(0, str(params.get('V_limit', '')))
        self.e_delay.delete(0, 'end'); self.e_delay.insert(0, str(params.get('delay', '')))
        self.e_cool.delete(0, 'end'); self.e_cool.insert(0, str(params.get('cooling_delay', '')))
        self.e_label.delete(0, 'end'); self.e_label.insert(0, params.get('label', ''))
        self.e_inom.delete(0, 'end'); self.e_inom.insert(0, str(params.get('I_nom', '')))
        self.e_ratio.delete(0, 'end'); self.e_ratio.insert(0, str(params.get('ratio', '')))
        self.e_turns.delete(0, 'end'); self.e_turns.insert(0, str(params.get('turns', 1.0)))
        self.stop_on_error_var.set(params.get('stop_on_error', False))
        self.e_error_threshold.delete(0, 'end'); self.e_error_threshold.insert(0, str(params.get('error_threshold', 1.0)))
        self.branch_var.set(params.get('branch', Branch.BOTH.value))
        self.preset_var.set(params.get('preset', DirectionPreset.DIVERGING.value))
        self.e_avg_count.delete(0, 'end'); self.e_avg_count.insert(0, str(params.get('averaging_count', DEFAULT_AVERAGING_COUNT)))
        self.e_avg_delay.delete(0, 'end'); self.e_avg_delay.insert(0, str(params.get('averaging_delay', DEFAULT_AVERAGING_DELAY)))
        self.discard_first_var.set(params.get('discard_first', DEFAULT_DISCARD_FIRST))
        self.adaptive_cooling_var.set(params.get('adaptive_cooling', False))

        self._on_excitation_change()
        self._append_log(f"Конфиг датчика загружен: {name}\n")
        messagebox.showinfo("Успех", f"Конфиг '{name}' загружен.")

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

        try:
            params = {
                "excitation_type": excitation_type,
                "output_type": self.output_var.get(),
                "X_start": num(self.e_start, "Начало"),
                "X_stop": num(self.e_stop, "Конец"),
                "X_step": num(self.e_step, "Шаг"),
                "delay": num(self.e_delay, "Задержка установки"),
                "cooling_delay": num(self.e_cool, "Задержка охлаждения"),
                "label": self.e_label.get().strip(),
            }
            if excitation_type == "current":
                params["V_limit"] = num(self.e_vlimit, "Огр. напряжения")
            else:
                params["V_limit"] = 0.0

            # Новые параметры
            params["I_nom"] = optional_num(self.e_inom)
            params["ratio"] = optional_num(self.e_ratio)
            params["turns"] = optional_num(self.e_turns) or 1.0
            params["stop_on_error"] = self.stop_on_error_var.get()
            params["error_threshold"] = optional_num(self.e_error_threshold) or 1.0
            params["branch"] = self.branch_var.get()
            params["preset"] = self.preset_var.get()
            params["averaging_count"] = int(optional_num(self.e_avg_count) or DEFAULT_AVERAGING_COUNT)
            params["averaging_delay"] = optional_num(self.e_avg_delay) or 0.0
            params["discard_first"] = self.discard_first_var.get()
            params["adaptive_cooling"] = self.adaptive_cooling_var.get()

            # I_nom — только метаданные датчика для шапки CSV, для измерения
            # он не нужен. А вот без коэффициента преобразования нечем считать
            # ожидаемый выход датчика, поэтому отсечка по погрешности без него
            # работать не может.
            if params["stop_on_error"] and params["ratio"] is None:
                raise ValueError("Для отсечки по погрешности необходимо указать коэффициент преобразования.")

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

        branch_text = {
            Branch.BOTH.value: "Обе полярности через реле",
            Branch.POSITIVE.value: "Только положительная полярность",
            Branch.NEGATIVE.value: "Только отрицательная полярность",
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
        warning = relay_current_warning(current_sweep_max_abs(params, params["excitation_type"]))
        if warning:
            confirm_text = f"⚠ {warning}\n\n{confirm_text}"

        if not messagebox.askyesno("Запуск измерения", confirm_text):
            return

        self.stop_event.clear()
        self._set_running(True)
        self._append_log(f"\n=== Измерение: {csv_path.name} ===\n")
        # Нужны на "done" для автопостроения графика (п.22) — там уже нет
        # доступа к полям формы (оператор мог их поменять, пока шло измерение).
        self._last_measure_params = params

        addr = {
            "dmm_addr": self.e_dmm.get().strip() or None,
            "src_addr": self.e_src.get().strip() or None,
            "relay_port": self.e_relay.get().strip() or None,
        }
        self.worker = threading.Thread(target=self._measure_worker, args=(params, csv_path, addr), daemon=True)
        self.worker.start()

    def _measure_worker(self, params, csv_path, addr):
        from visa_backend import make_resource_manager
        from orchestrate import run_measurement_session

        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = _QueueWriter(self.events)
        rm = None
        try:
            rm = make_resource_manager()
            run_measurement_session(
                rm, params, csv_path,
                dmm_addr=addr["dmm_addr"], src_addr=addr["src_addr"], relay_port=addr["relay_port"],
                should_stop=self.stop_event.is_set,
                on_session_open=self._session.set,
            )
            self.events.put(("done", str(csv_path)))
        except Exception as e:
            traceback.print_exc()
            self.events.put(("error", str(e)))
        finally:
            # Приборы этой сессии закрыты — ручка больше не должна вести на
            # мёртвые сессии, иначе следующий «Стоп» попытается писать в них.
            self._session.clear()
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
        state = "disabled" if self.auto_range_var.get() else "normal"
        for entry in self._range_entries:
            entry.configure(state=state)

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
        I_nom, X = metadata_i_nom_and_ratio(path)
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
        self.notebook.select(1)

    def _do_estimate_ratio(self):
        """п.10 (BETA): фактический коэффициент по снятым точкам, отдельно от построения графика."""
        from analysis import estimate_ratio_from_data

        csv_path = self._resolve_plot_csv_path()
        if csv_path is None:
            return
        df = self._current_df if self._current_df is not None and self.plot_csv_path == csv_path \
            else pd.read_csv(csv_path, comment='#')

        try:
            result = estimate_ratio_from_data(df)
        except ValueError as e:
            messagebox.showerror("Не удалось определить коэффициент", str(e))
            return

        messagebox.showinfo(
            "Коэффициент преобразования (BETA)",
            f"Фактический: 1:{result['X_actual']:.2f}\n"
            f"Округлённый (кратно 50): 1:{result['X_rounded']:.0f}\n"
            f"Расхождение: {result['discrepancy_percent']:.2f}%",
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
        from analysis import save_dataframe_with_metadata
        save_dataframe_with_metadata(self.plot_csv_path, self._current_df)
        self._append_log(f"Изменения точек сохранены: {self.plot_csv_path}\n")
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
            I_nom, X = metadata_i_nom_and_ratio(output_path)
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
        path = export_xlsx(csv_path, xlsx_path=Path(out))
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
        self.notebook.select(1)

    def _embed_figure(self, fig):
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        import matplotlib.pyplot as plt

        for w in self.plot_frame.winfo_children():
            w.destroy()
        if self._current_fig is not None:
            plt.close(self._current_fig)
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
        self.stop_event.set()
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
        if self._current_fig is not None:
            try:
                import matplotlib.pyplot as plt
                plt.close(self._current_fig)
            except Exception:
                pass
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
                    ok, status, detail = payload
                    self._preflight_ok = ok
                    self._set_status(status, "ok" if ok else "error")
                    self.footer_label.configure(
                        text=("Готово к измерению." if ok
                              else "Измерение заблокировано. См. журнал / установите NI-VISA."))
                    self.start_btn.configure(state="normal" if ok else "disabled")
                elif kind == "done":
                    self.last_csv = payload
                    self._append_log(f"\n✔ Измерение завершено. Данные: {payload}\n")
                    self._set_running(False)
                    self._set_status("Измерение завершено", "ok")
                    self._auto_plot_after_measurement(Path(payload))
                elif kind == "error":
                    self._append_log(f"\n✖ Ошибка: {payload}\n")
                    self._set_running(False)
                    self._set_status("Ошибка измерения", "error")
                    messagebox.showerror("Ошибка измерения", payload)
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
