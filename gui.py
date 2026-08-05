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

from apppaths import default_data_dir
from config import ConfigManager, SensorConfigManager
from cli import make_csv_filename, validate_measure_params
from measurement import EXCITATION_UNITS


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
        self.sensor_config_mgr = SensorConfigManager(self.data_dir)

        self.events = queue.Queue()
        self.stop_event = threading.Event()
        # Ручка на приборы идущего измерения — через неё «Стоп» обесточивает
        # стенд, не дожидаясь, пока цикл дойдёт до проверки stop_event.
        self._session = _SessionSlot()
        self.worker = None
        self._preflight_ok = False
        self.last_csv = None
        self._current_fig = None

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

        ttk.Label(pf, text="Комментарий").grid(row=8, column=0, sticky="w", pady=(6, 0))
        self.e_label = ttk.Entry(pf)
        self.e_label.grid(row=8, column=1, columnspan=2, sticky="ew", pady=(6, 0))

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

        self.no_relay_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Измерять без реле (одна полярность)",
                        variable=self.no_relay_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

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
        plot_tab.rowconfigure(1, weight=1)
        plot_tab.columnconfigure(0, weight=1)

        an = ttk.Frame(plot_tab)
        an.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(an, text="I ном., А").grid(row=0, column=0, sticky="w")
        self.e_inom = ttk.Entry(an, width=10)
        self.e_inom.grid(row=0, column=1, padx=(6, 14))
        ttk.Label(an, text="Коэфф. 1:X").grid(row=0, column=2, sticky="w")
        self.e_ratio = ttk.Entry(an, width=10)
        self.e_ratio.grid(row=0, column=3, padx=(6, 14))
        ttk.Button(an, text="Построить график", command=self._do_analyze).grid(row=0, column=4)

        self.plot_frame = ttk.Frame(plot_tab, style="Card.TFrame")
        self.plot_frame.grid(row=1, column=0, sticky="nsew")
        self.plot_frame.rowconfigure(0, weight=1)
        self.plot_frame.columnconfigure(0, weight=1)
        self.plot_hint = ttk.Label(self.plot_frame, style="Muted.TLabel",
                                   text="После измерения задайте I ном. и X, затем «Построить график».\n"
                                        "Либо строится по последнему CSV в папке data.")
        self.plot_hint.grid(row=0, column=0)

        self.notebook = nb

    # ---------------------------------------------------------------- helpers
    def _prefill_from_config(self):
        saved = self.config_mgr.load()
        if not saved:
            return
        if saved.get("excitation_type") in ("current", "voltage"):
            self.excitation_var.set(saved["excitation_type"])
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
        params['use_relay'] = not self.no_relay_var.get()

        path = self.sensor_config_mgr.save_sensor_config(name, params)
        self._append_log(f"Конфиг датчика сохранён: {path}\n")
        messagebox.showinfo("Успех", f"Конфиг сохранён как '{name}'.")

    def _load_config(self):
        name = self.e_config_name.get().strip()
        if not name:
            messagebox.showwarning("Имя конфига", "Введите имя конфига для загрузки.")
            return
        params = self.sensor_config_mgr.load_sensor_config(name)
        if params is None:
            messagebox.showerror("Ошибка", f"Конфиг '{name}' не найден или повреждён.")
            return

        # Заполняем поля интерфейса
        self.excitation_var.set(params.get('excitation_type', 'current'))
        self.e_start.delete(0, 'end'); self.e_start.insert(0, str(params.get('X_start', '')))
        self.e_stop.delete(0, 'end'); self.e_stop.insert(0, str(params.get('X_stop', '')))
        self.e_step.delete(0, 'end'); self.e_step.insert(0, str(params.get('X_step', '')))
        self.e_vlimit.delete(0, 'end'); self.e_vlimit.insert(0, str(params.get('V_limit', '')))
        self.e_delay.delete(0, 'end'); self.e_delay.insert(0, str(params.get('delay', '')))
        self.e_cool.delete(0, 'end'); self.e_cool.insert(0, str(params.get('cooling_delay', '')))
        self.e_label.delete(0, 'end'); self.e_label.insert(0, params.get('label', ''))
        self.e_inom.delete(0, 'end'); self.e_inom.insert(0, str(params.get('I_nom', '')))
        self.e_ratio.delete(0, 'end'); self.e_ratio.insert(0, str(params.get('ratio', '')))
        self.stop_on_error_var.set(params.get('stop_on_error', False))
        self.e_error_threshold.delete(0, 'end'); self.e_error_threshold.insert(0, str(params.get('error_threshold', 1.0)))
        self.no_relay_var.set(not params.get('use_relay', True))

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
            params["stop_on_error"] = self.stop_on_error_var.get()
            params["error_threshold"] = optional_num(self.e_error_threshold) or 1.0
            params["use_relay"] = not self.no_relay_var.get()

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

        if not messagebox.askyesno(
            "Запуск измерения",
            f"Возбуждение: {params['excitation_type']}\n"
            f"Диапазон: {params['X_start']}..{params['X_stop']} (шаг {params['X_step']})\n"
            f"Обе полярности через реле.\n\nЗапустить измерение?",
        ):
            return

        self.stop_event.clear()
        self._set_running(True)
        self._append_log(f"\n=== Измерение: {csv_path.name} ===\n")

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

    # ------------------------------------------------------------------ analyze
    def _do_analyze(self):
        from analysis import load_and_analyze, find_latest_csv

        csv_path = self.last_csv
        try:
            if not csv_path:
                csv_path = find_latest_csv(self.data_dir)
        except FileNotFoundError as e:
            messagebox.showerror("Нет данных", str(e))
            return

        try:
            inom = float(self.e_inom.get().strip().replace(",", "."))
            ratio = float(self.e_ratio.get().strip().replace(",", "."))
            if inom <= 0 or ratio <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Проверьте параметры анализа",
                                 "I ном. и коэффициент X должны быть положительными числами.")
            return

        try:
            stats = load_and_analyze(Path(csv_path), I_nom=inom, X=ratio,
                                     save_png=True, show=False, close_fig=False)
        except Exception as e:
            messagebox.showerror("Ошибка анализа", str(e))
            return

        self._append_log(
            f"[Анализ] {Path(csv_path).name}: макс. погрешность {stats['max_error_percent']:.4f} %, "
            f"средняя {stats['mean_error_percent']:.4f} %; PNG: {stats['png_path']}\n"
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
