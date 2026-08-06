import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import ConfigManager
from limits import (
    relay_current_block_reason,
    relay_current_warning,
    strictest_current_source_limits,
    strictest_voltage_source_limits,
    voltage_ceiling_block_reason,
    smooth_ramp_block_reason,
)
from measurement import (
    DEFAULT_AVERAGING_COUNT, DEFAULT_AVERAGING_DELAY, DEFAULT_DISCARD_FIRST,
    DEFAULT_ADAPTIVE_COOLING_MIN_DELAY, DEFAULT_ADAPTIVE_COOLING_MAX_DELAY,
)
from sweep import Branch, DirectionPreset


def build_parser(default_data_dir: Path = Path("data")) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="IVTrace",
        description="IVtrace — автоматизация снятия амплитудной характеристики датчиков тока/напряжения. "
                    "Без аргументов запускается графический интерфейс (GUI).",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=default_data_dir,
        help="Каталог для хранения CSV/PNG и конфига (по умолчанию: ./data рядом с программой)",
    )
    parser.add_argument(
        "--skip-selftest", action="store_true",
        help="Пропустить предполётные самотесты (НЕ рекомендуется: тесты защищают оборудование от повреждения при поломке кода)",
    )
    parser.add_argument(
        "--suppress-warnings", action="store_true",
        help="Отключить необязательные предупреждения и уведомления (НЕ рекомендуется): "
             "поверка приборов, перепутанная полярность, превышение 400 А. "
             "Жёсткий запрет 800 А, аварийный останов и отсечку по погрешности НЕ отключает (п.38).",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    # ---------------- gui ----------------
    subparsers.add_parser(
        "gui",
        help="Запустить графический интерфейс (то же, что запуск без аргументов)",
    )

    # ---------------- selftest ----------------
    subparsers.add_parser(
        "selftest",
        help="Прогнать виртуальные самотесты и проверку NI-VISA, вывести отчёт и выйти",
    )

    # ---------------- discover ----------------
    subparsers.add_parser(
        "discover",
        help="Разовый поиск приборов и платы реле (Ф4, п.25) — диагностика без запуска измерения",
    )

    # ---------------- relay ----------------
    p_relay = subparsers.add_parser(
        "relay",
        help="Ручное управление платой реле напрямую, вне измерительного цикла (Ф4, п.13)",
    )
    p_relay.add_argument("direction", choices=["forward", "reverse", "off"],
                         help="forward — прямое направление (IFW), reverse — обратное (IRW), off — разомкнуть (I_0)")
    p_relay.add_argument("--relay-port", type=str, default=None,
                         help="Serial-порт платы реле, например COM3 (пропустить автоопределение)")
    p_relay.add_argument("--yes", action="store_true",
                         help="Не спрашивать подтверждения перед переключением")

    # ---------------- setpoint ----------------
    p_setpoint = subparsers.add_parser(
        "setpoint",
        help="Прямая знаковая уставка вне измерительного цикла (Ф5, п.40) — держит значение до Enter/Ctrl+C",
    )
    p_setpoint.add_argument("value", type=float,
                            help="Значение со знаком: >0 — прямое направление, <0 — обратное, 0 — выключить")
    p_setpoint.add_argument("--excitation", choices=["current", "voltage"], default="current")
    p_setpoint.add_argument("--vlimit", type=float, default=None,
                            help="Ограничение напряжения источника (обязательно для --excitation current)")
    p_setpoint.add_argument("--ilimit", type=float, default=None,
                            help="Ограничение тока источника напряжения (обязательно для --excitation voltage)")
    p_setpoint.add_argument("--dmm-addr", type=str, default=None)
    p_setpoint.add_argument("--src-addr", type=str, default=None)
    p_setpoint.add_argument("--relay-port", type=str, default=None)
    p_setpoint.add_argument("--yes", action="store_true", help="Не спрашивать подтверждения")

    # ---------------- identify ----------------
    p_identify = subparsers.add_parser(
        "identify",
        help="«Мигнуть» прибором по VISA-адресу, если у него в конфиге настроена identify_command (Ф4, п.11)",
    )
    p_identify.add_argument("address", help="VISA-адрес (см. discover)")

    # ---------------- profile ----------------
    p_profile = subparsers.add_parser("profile", help="Профили датчиков (п.39)")
    profile_sub = p_profile.add_subparsers(dest="profile_command", required=True)

    p_profile_list = profile_sub.add_parser("list", help="Список сохранённых профилей")
    p_profile_list.add_argument("--excitation", choices=["current", "voltage"], default=None,
                                help="Сузить список до одного типа возбуждения (по умолчанию — оба)")

    p_profile_delete = profile_sub.add_parser("delete", help="Удалить профиль")
    p_profile_delete.add_argument("name")
    p_profile_delete.add_argument("--excitation", choices=["current", "voltage"], default=None)
    p_profile_delete.add_argument("--yes", action="store_true")

    p_profile_rename = profile_sub.add_parser("rename", help="Переименовать профиль")
    p_profile_rename.add_argument("old_name")
    p_profile_rename.add_argument("new_name")
    p_profile_rename.add_argument("--excitation", choices=["current", "voltage"], default=None)

    # ---------------- calibration ----------------
    p_cal = subparsers.add_parser("calibration", help="Поверка приборов — реестр физических экземпляров (п.3-UI, бага 6/7)")
    cal_sub = p_cal.add_subparsers(dest="calibration_command", required=True)

    cal_sub.add_parser("list", help="Статус поверки всех известных моделей и заведённых в реестре приборов")

    p_cal_set = cal_sub.add_parser("set", help="Завести/обновить запись поверки прибора в реестре")
    p_cal_set.add_argument("model_id",
                           help="Идентификатор модели (см. поле model_id конфига или calibration list)")
    p_cal_set.add_argument("--serial", default="",
                           help="Серийный номер физического экземпляра — пусто, если экземпляр единственный")
    p_cal_set.add_argument("--date", required=True, help="Дата последней поверки, ISO YYYY-MM-DD")
    p_cal_set.add_argument("--interval-months", type=int, required=True)
    p_cal_set.add_argument("--comment", default="")

    p_cal_del = cal_sub.add_parser("delete", help="Удалить запись поверки прибора из реестра")
    p_cal_del.add_argument("model_id")
    p_cal_del.add_argument("--serial", default="")

    # ---------------- config ----------------
    p_config = subparsers.add_parser("config", help="Настройки приложения (п.23 — рабочая папка)")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)

    config_sub.add_parser("show", help="Текущая рабочая папка и её источник (переопределена/по умолчанию)")

    p_config_set = config_sub.add_parser("set-work-dir", help="Переопределить рабочую папку")
    p_config_set.add_argument("path", type=Path)

    config_sub.add_parser("reset-work-dir", help="Сбросить переопределение рабочей папки (снова по умолчанию)")

    # ---------------- measure ----------------
    p_measure = subparsers.add_parser(
        "measure",
        help="Выполнить измерение амплитудной характеристики (обе полярности через реле)",
    )
    p_measure.add_argument(
        "--excitation", choices=["current", "voltage"], default=None,
        help="Тип возбуждения датчика: current (источник тока) или voltage (источник напряжения, "
             "BETA — рабочий предел 60 В, см. --output)",
    )
    p_measure.add_argument(
        "--output", choices=["current", "voltage"], default=None,
        help="Что измеряет мультиметр на выходе датчика: current (по умолчанию, амперметр) "
             "или voltage (вольтметр, BETA — не проверено на реальном стенде) — независимо "
             "от --excitation, любая комбинация допустима (ось А-1, PLAN_V2.md)",
    )
    p_measure.add_argument("--start", type=float, help="Начальное значение возбуждения (обычно 0)")
    p_measure.add_argument("--stop", type=float, help="Конечное значение возбуждения")
    p_measure.add_argument("--step", type=float, help="Шаг возбуждения")
    p_measure.add_argument(
        "--custom-program", type=str, default=None,
        help="Планировщик кастомных программ (BETA): свободный порядок точек/диапазонов через "
             "запятую, например '-25, 0:40:10, -15, +5' — заменяет --start/--stop/--step/--branch/--preset",
    )
    p_measure.add_argument("--vlimit", type=float, help="Ограничение напряжения на источнике тока, В (не используется для источника напряжения)")
    p_measure.add_argument("--ilimit", type=float, help="Ограничение тока на источнике напряжения, А (не используется для источника тока)")
    p_measure.add_argument("--delay", type=float, help="Задержка на установку возбуждения, с")
    p_measure.add_argument("--cool", type=float, help="Задержка на охлаждение между точками, с")
    p_measure.add_argument("--label", type=str, help="Комментарий (датчик, пометка)")
    p_measure.add_argument(
        "--smooth-ramp", action="store_true",
        help="Плавное нарастание тока между точками (BETA, только ток, до 300 А) — "
             "заменяет --delay/--cool, задавайте --ramp-duration",
    )
    p_measure.add_argument(
        "--ramp-duration", type=float, default=None,
        help="Время перехода между точками при --smooth-ramp, с",
    )
    p_measure.add_argument(
        "--dmm-addr", type=str, default=None,
        help="VISA-адрес мультиметра (пропустить автоопределение)",
    )
    p_measure.add_argument(
        "--src-addr", type=str, default=None,
        help="VISA-адрес источника (пропустить автоопределение)",
    )
    p_measure.add_argument(
        "--relay-port", type=str, default=None,
        help="Serial-порт платы реле, например COM3 (пропустить автоопределение)",
    )
    p_measure.add_argument(
        "--yes", action="store_true",
        help="Не спрашивать подтверждения, использовать сохранённые/переданные параметры без диалога",
    )

    # --- Новые параметры ---
    p_measure.add_argument(
        "--inom", type=float, default=None,
        help="Номинальный первичный ток датчика, А",
    )
    p_measure.add_argument(
        "--ratio", type=float, default=None,
        help="Коэффициент преобразования 1:X (передать X)",
    )
    p_measure.add_argument(
        "--zero-offset", type=float, default=None,
        help="Известное смещение нуля датчика (вычитается при расчёте погрешности)",
    )
    p_measure.add_argument(
        "--error-threshold", type=float, default=None,
        help="Порог погрешности для досрочной остановки, %% (по умолчанию 1.0)",
    )
    p_measure.add_argument(
        "--stop-on-error", action="store_true",
        help="Остановить измерение при превышении порога погрешности",
    )
    p_measure.add_argument(
        "--branch", choices=[b.value for b in Branch], default=None,
        help="Какая полярность измеряется: both (обе, через реле, по умолчанию), "
             "positive или negative (только одна)",
    )
    p_measure.add_argument(
        "--preset", choices=[p.value for p in DirectionPreset], default=None,
        help="Схема прохода при --branch both (по умолчанию diverging — как в v1.4ae): "
             "diverging (0→+X, затем 0→−X), converging (+X→0, затем 0→−X, непрерывно через ноль), "
             "descending (+X→0, затем −X→0), full_cycle (0→+X→0→−X→0, петля гистерезиса)",
    )
    p_measure.add_argument(
        "--turns", type=float, default=None,
        help="Число витков провода через окно датчика (по умолчанию 1). "
             "Реальный вход датчика = уставка × витки; в провод и реле при этом "
             "идёт сама уставка, не умноженная на витки. Только для возбуждения током.",
    )
    p_measure.add_argument(
        "--avg-count", type=int, default=None,
        help="Число отсчётов на усреднение одной точки (по умолчанию 4)",
    )
    p_measure.add_argument(
        "--avg-delay", type=float, default=None,
        help="Задержка между отсчётами усреднения, с (по умолчанию 0)",
    )
    p_measure.add_argument(
        "--avg-keep-first", action="store_true",
        help="Не отбрасывать первый отсчёт усреднения (по умолчанию отбрасывается — "
             "защита от случая, когда авто-диапазон ещё не устаканился)",
    )
    p_measure.add_argument(
        "--adaptive-cooling", action="store_true",
        help="BETA: задержка охлаждения растёт квадратично с током вместо фиксированной "
             "(джоулево тепло ~ I^2). Алгоритм не проверен на реальном стенде.",
    )
    p_measure.add_argument(
        "--adaptive-cooling-min-delay", type=float, default=None,
        help="Задержка охлаждения на нулевой точке при --adaptive-cooling, с "
             f"(по умолчанию {DEFAULT_ADAPTIVE_COOLING_MIN_DELAY:.1f})",
    )
    p_measure.add_argument(
        "--adaptive-cooling-max-delay", type=float, default=None,
        help="Задержка охлаждения на самой большой точке развёртки при --adaptive-cooling, с "
             f"(по умолчанию {DEFAULT_ADAPTIVE_COOLING_MAX_DELAY:.1f})",
    )
    p_measure.add_argument(
        "--save-config", type=str, default=None,
        help="Сохранить текущие параметры как конфиг датчика с указанным именем",
    )
    p_measure.add_argument(
        "--load-config", type=str, default=None,
        help="Загрузить параметры из конфига датчика",
    )

    # ---------------- analyze ----------------
    p_analyze = subparsers.add_parser("analyze", help="Построить график и рассчитать погрешность по указанному (или последнему) CSV")
    p_analyze.add_argument("--file", type=Path, default=None, help="Путь к конкретному CSV (по умолчанию — последний в data-dir; п.20)")
    p_analyze.add_argument("--inom", type=float, default=None, help="Номинальный первичный ток датчика, А")
    p_analyze.add_argument("--ratio", type=float, default=None, help="Коэффициент преобразования 1:X (передать X)")
    p_analyze.add_argument("--zero-offset", type=float, default=None,
                           help="Смещение нуля датчика — переопределяет значение из шапки CSV, если оно там есть")
    p_analyze.add_argument("--no-show", action="store_true", help="Не открывать окно с графиком (только сохранить PNG)")
    p_analyze.add_argument("--labels", action="store_true", help="Подписывать погрешность над каждой точкой на графике (п.30)")
    p_analyze.add_argument("--xlsx", action="store_true", help="Экспортировать результаты в XLSX рядом с CSV (п.21)")
    p_analyze.add_argument(
        "--estimate-ratio", action="store_true",
        help="BETA: определить фактический коэффициент преобразования по снятым точкам (МНК, п.10), не строя график",
    )

    return parser


# ----------------------------------------------------------------------
# Интерактивный ввод параметров measure (с подсказками из сохранённого конфига)
# ----------------------------------------------------------------------

def current_sweep_max_abs(params: dict, excitation_type: str) -> Optional[float]:
    """
    Наибольший по модулю ток, который реально пройдёт через провод и плату
    реле за время измерения — то, что нужно сверять с лимитами limits.py.

    Намеренно НЕ учитывает число витков (см. PLAN_V2.md, п. 37, появится в
    Ф2): через реле течёт уставка источника, а не уставка, умноженная на
    витки — витки умножают ампервитки внутри датчика, а не ток в проводе.
    Когда параметр витков появится в params, здесь ничего менять не нужно —
    менять нужно было бы в противоположном месте, если бы кто-то по ошибке
    стал сверять лимит с X_set * turns.

    None для возбуждения напряжением или при неполных params — сверять
    нечего.

    custom_program (feature "планировщик кастомных программ") — если
    задан, X_start/X_stop не имеют смысла вовсе, максимум берётся из
    разбора самой программы (см. sweep.parse_custom_program). Ошибка
    разбора здесь не поднимается — просто None (сама ошибка формулируется
    отдельно, в validate_measure_params, чтобы не дублировать сообщение).
    """
    if excitation_type != 'current':
        return None
    custom_program = params.get('custom_program')
    if custom_program:
        try:
            from sweep import parse_custom_program
            values = parse_custom_program(custom_program)
        except ValueError:
            return None
        return max((abs(v) for v in values), default=None)
    X_start, X_stop = params.get('X_start'), params.get('X_stop')
    if X_start is None or X_stop is None:
        return None
    return max(abs(X_start), abs(X_stop))


def validate_measure_params(params: dict, excitation_type: str,
                             current_source_limits: Optional[dict] = None,
                             voltage_source_limits: Optional[dict] = None) -> list:
    """
    current_source_limits/voltage_source_limits — паспортные пределы
    источника (см. limits.strictest_current_source_limits /
    strictest_voltage_source_limits). По умолчанию читаются из
    instruments/{current,voltage}_sources/*.json; параметры существуют в
    основном для тестов — чтобы не зависеть от содержимого реальных
    конфигов на диске.
    """
    errors = []
    custom_program = params.get('custom_program')
    if custom_program:
        # Планировщик кастомных программ (feature): X_start/X_stop/X_step
        # не имеют смысла вовсе — проверяется сам текст программы, ошибка
        # разбора (sweep.parse_custom_program) становится ошибкой валидации.
        from sweep import parse_custom_program
        try:
            parse_custom_program(custom_program)
        except ValueError as e:
            errors.append(str(e))
    else:
        if params.get('X_step') is None or params['X_step'] <= 0:
            errors.append("Шаг возбуждения должен быть положительным числом.")
        if params.get('X_start') is None or params.get('X_stop') is None:
            errors.append("Начальное и конечное значения должны быть заданы.")
        # X_stop < X_start больше НЕ ошибка (п.17): планировщик (sweep.py)
        # поддерживает любой знак и порядок X_start/X_stop — 250→0 (по модулю
        # убывающий проход) не менее корректен, чем 0→250. Порядок точек
        # внутри развёртки определяет sweep.plan_sweep(), не эта проверка.
    if params.get('turns') is not None and params['turns'] <= 0:
        errors.append("Число витков должно быть положительным числом.")
    if params.get('delay') is not None and params['delay'] < 0:
        errors.append("Задержка на установку не может быть отрицательной.")
    if params.get('cooling_delay') is not None and params['cooling_delay'] < 0:
        errors.append("Задержка на охлаждение не может быть отрицательной.")
    if params.get('adaptive_cooling'):
        min_delay = params.get('adaptive_cooling_min_delay')
        max_delay = params.get('adaptive_cooling_max_delay')
        if min_delay is None or min_delay < 0:
            errors.append("Минимальная задержка охлаждения не может быть отрицательной.")
        if max_delay is None or max_delay < 0:
            errors.append("Максимальная задержка охлаждения не может быть отрицательной.")
        if min_delay is not None and max_delay is not None and min_delay > max_delay:
            errors.append(
                "Минимальная задержка охлаждения не может быть больше максимальной."
            )
    if excitation_type == 'current' and (params.get('V_limit') is None or params['V_limit'] <= 0):
        errors.append("Ограничение напряжения должно быть положительным числом.")
    if excitation_type == 'voltage' and (params.get('I_limit') is None or params['I_limit'] <= 0):
        errors.append("Ограничение тока должно быть положительным числом.")
    if excitation_type == 'voltage' and params.get('smooth_ramp'):
        errors.append("Плавное нарастание (BETA) доступно только для возбуждения током.")

    if excitation_type == 'current':
        # Лимиты платы реле (limits.py) — про физические контакты реле,
        # которых в режиме "No Relay" в цепи попросту нет (стенд без платы
        # коммутации вовсе, см. sweep.Branch.NO_RELAY) — их проверять
        # бессмысленно. Паспортный предел самого источника (ниже) остаётся
        # в силе независимо от режима — это ограничение источника, не реле.
        if params.get('branch') != Branch.NO_RELAY.value:
            block = relay_current_block_reason(current_sweep_max_abs(params, excitation_type))
            if block:
                errors.append(block)

        if params.get('smooth_ramp'):
            ramp_block = smooth_ramp_block_reason(current_sweep_max_abs(params, excitation_type))
            if ramp_block:
                errors.append(ramp_block)
            if params.get('ramp_duration') is None or params['ramp_duration'] <= 0:
                errors.append("Время шага плавного нарастания должно быть положительным числом.")

        if current_source_limits is None:
            current_source_limits = strictest_current_source_limits()
        max_v = current_source_limits.get('max_voltage')
        if max_v is not None and params.get('V_limit') is not None and params['V_limit'] > max_v:
            errors.append(
                f"Ограничение напряжения {params['V_limit']} В превышает паспортный предел "
                f"источника ({max_v} В) — физически недостижимо."
            )
        max_i = current_source_limits.get('max_current')
        max_abs = current_sweep_max_abs(params, excitation_type)
        if max_i is not None and max_abs is not None and max_abs > max_i:
            errors.append(
                f"Уставка тока {max_abs:.1f} А превышает паспортный предел источника "
                f"({max_i:.1f} А) — физически недостижимо."
            )

    if excitation_type == 'voltage':
        if voltage_source_limits is None:
            voltage_source_limits = strictest_voltage_source_limits()
        max_v = voltage_source_limits.get('max_voltage')
        # Наибольшая ПО МОДУЛЮ уставка — источник не умеет отрицательное
        # напряжение напрямую, знак всегда отрабатывает реле (как и для
        # тока, см. current_sweep_max_abs), поэтому и X_start, и X_stop
        # в любом порядке/с любым знаком могут оказаться "тем самым"
        # максимумом (например X_start=-60, X_stop=0).
        X_start, X_stop = params.get('X_start'), params.get('X_stop')
        max_abs_v = max(abs(X_start), abs(X_stop)) if X_start is not None and X_stop is not None else None
        if max_v is not None and max_abs_v is not None and max_abs_v > max_v:
            errors.append(
                f"Уставка напряжения {max_abs_v} В превышает паспортный предел источника "
                f"({max_v} В) — физически недостижимо."
            )
        # Рабочий потолок 60 В (п.35) — отдельно от паспортного предела
        # конкретного источника, см. limits.voltage_ceiling_block_reason.
        ceiling_block = voltage_ceiling_block_reason(max_abs_v)
        if ceiling_block:
            errors.append(ceiling_block)

        max_i_limit = voltage_source_limits.get('max_current_limit')
        if max_i_limit is not None and params.get('I_limit') is not None and params['I_limit'] > max_i_limit:
            errors.append(
                f"Ограничение тока {params['I_limit']} А превышает паспортный предел "
                f"источника ({max_i_limit} А) — физически недостижимо."
            )

    return errors


def _prompt_float(prompt: str, validator=None, error_msg: str = None) -> float:
    while True:
        try:
            value = float(input(prompt))
        except ValueError:
            print("Ошибка ввода: введите число. Попробуйте снова.")
            continue
        if validator is not None and not validator(value):
            print(error_msg or "Недопустимое значение. Попробуйте снова.")
            continue
        return value


def _prompt_excitation_type() -> str:
    while True:
        choice = input("Тип возбуждения датчика — ток или напряжение? (current/voltage, c/v): ").strip().lower()
        if choice in ('current', 'c', 'ток', 'т'):
            return 'current'
        if choice in ('voltage', 'v', 'напряжение', 'н'):
            return 'voltage'
        print("Введите 'current'/'c' или 'voltage'/'v'.")


def resolve_measure_params(args, config_mgr: ConfigManager, sensor_config_mgr=None) -> dict:
    """
    Заполняет параметры измерения: сперва из аргументов командной строки,
    затем (если чего-то не хватает) — из сохранённого конфига или интерактивного ввода.
    Обновляет конфиг сохранёнными значениями.

    Если указан --load-config, параметры загружаются из указанного конфига.
    Если указан --save-config, после заполнения параметры сохраняются в конфиг.
    """
    saved = config_mgr.load()
    loaded = {}

    # Загрузка конфига датчика, если указан
    if args.load_config and sensor_config_mgr:
        loaded = sensor_config_mgr.load_sensor_config(args.load_config) or {}
        if loaded:
            print(f"Конфиг датчика '{args.load_config}' загружен.")
        else:
            print(f"Предупреждение: конфиг '{args.load_config}' не найден или повреждён.")

    # --- excitation type — спрашиваем в первую очередь ---
    excitation_type = args.excitation or loaded.get('excitation_type')
    if excitation_type is None:
        last_excitation = saved.get('excitation_type') if saved else None
        if last_excitation and args.yes:
            excitation_type = last_excitation
        elif last_excitation:
            hint = 'ток' if last_excitation == 'current' else 'напряжение'
            use_prev = input(f"Последний раз использовалось возбуждение: {hint}. Использовать снова? (y/n, по умолчанию y): ").strip().lower()
            if use_prev != 'n':
                excitation_type = last_excitation
        if excitation_type is None:
            excitation_type = _prompt_excitation_type()

    unit = 'А' if excitation_type == 'current' else 'В'

    # Формируем словарь параметров: приоритет — аргументы командной строки, затем загруженный конфиг, затем сохранённый
    params = {
        'excitation_type': excitation_type,
        # output_type (ось А-1) — не переспрашивается интерактивно и не
        # наследуется от прошлого запуска, в отличие от excitation_type:
        # тихий дефолт 'current' сохраняет прежнее поведение для всех, кто
        # флаг не передавал (это подавляющее большинство существующих
        # сценариев — датчик тока с выходом по току).
        'output_type': args.output if args.output is not None else loaded.get('output_type', 'current'),
        # п.38: не сохраняется/не наследуется от прошлого запуска — это
        # свойство ТЕКУЩЕГО запуска CLI (флаг верхнего уровня), а не режима
        # измерения датчика.
        'suppress_notifications': getattr(args, 'suppress_warnings', False),
        'X_start': args.start if args.start is not None else loaded.get('X_start'),
        'X_stop': args.stop if args.stop is not None else loaded.get('X_stop'),
        'X_step': args.step if args.step is not None else loaded.get('X_step'),
        'custom_program': args.custom_program if args.custom_program is not None else loaded.get('custom_program'),
        'V_limit': args.vlimit if args.vlimit is not None else loaded.get('V_limit'),
        'I_limit': args.ilimit if args.ilimit is not None else loaded.get('I_limit'),
        'delay': args.delay if args.delay is not None else loaded.get('delay'),
        'cooling_delay': args.cool if args.cool is not None else loaded.get('cooling_delay'),
        'label': args.label if args.label is not None else loaded.get('label'),
        # Новые параметры
        'I_nom': args.inom if args.inom is not None else loaded.get('I_nom'),
        'ratio': args.ratio if args.ratio is not None else loaded.get('ratio'),
        'zero_offset': args.zero_offset if args.zero_offset is not None else loaded.get('zero_offset', 0.0),
        'error_threshold': args.error_threshold if args.error_threshold is not None else loaded.get('error_threshold', 1.0),
        'stop_on_error': args.stop_on_error or loaded.get('stop_on_error', False),
        'branch': args.branch if args.branch is not None else loaded.get('branch', Branch.BOTH.value),
        'preset': args.preset if args.preset is not None else loaded.get('preset', DirectionPreset.DIVERGING.value),
        'turns': args.turns if args.turns is not None else loaded.get('turns', 1.0),
        'averaging_count': args.avg_count if args.avg_count is not None else loaded.get('averaging_count', DEFAULT_AVERAGING_COUNT),
        'averaging_delay': args.avg_delay if args.avg_delay is not None else loaded.get('averaging_delay', DEFAULT_AVERAGING_DELAY),
        # --avg-keep-first — это store_true, он НИКОГДА не бывает None (см.
        # тот же нюанс, что раньше был с --no-relay): флаг может только
        # ВЫКЛЮЧИТЬ отбрасывание первого отсчёта, а включить — конфиг.
        'discard_first': loaded.get('discard_first', DEFAULT_DISCARD_FIRST) and not args.avg_keep_first,
        'adaptive_cooling': args.adaptive_cooling or loaded.get('adaptive_cooling', False),
        'smooth_ramp': args.smooth_ramp or loaded.get('smooth_ramp', False),
        'ramp_duration': args.ramp_duration if args.ramp_duration is not None else loaded.get('ramp_duration', 1.0),
        'adaptive_cooling_min_delay': (
            args.adaptive_cooling_min_delay if args.adaptive_cooling_min_delay is not None
            else loaded.get('adaptive_cooling_min_delay', DEFAULT_ADAPTIVE_COOLING_MIN_DELAY)
        ),
        'adaptive_cooling_max_delay': (
            args.adaptive_cooling_max_delay if args.adaptive_cooling_max_delay is not None
            else loaded.get('adaptive_cooling_max_delay', DEFAULT_ADAPTIVE_COOLING_MAX_DELAY)
        ),
    }

    # Для источника напряжения V_limit не используется, и наоборот — I_limit
    # не используется для источника тока (симметрично, баг-репорт).
    if excitation_type == 'voltage':
        params['V_limit'] = 0.0
    else:
        params['I_limit'] = 0.0

    # Планировщик кастомных программ (BETA) заменяет X_start/X_stop/X_step
    # текстом программы — эти три поля становятся ненужными вовсе. Плавное
    # нарастание (BETA) заменяет delay/cooling_delay ramp_duration — эти
    # два переключателя независимы друг от друга (custom_program можно
    # сочетать со smooth_ramp: план другой, но задержки/охлаждение внутри
    # плана работают как обычно, если ramp не включён). Проверка полноты
    # параметров должна спрашивать про НУЖНОЕ для текущей комбинации
    # режимов, а не про всё сразу.
    numeric_keys = ['custom_program'] if params.get('custom_program') else ['X_start', 'X_stop', 'X_step']
    if params.get('smooth_ramp'):
        numeric_keys.append('ramp_duration')
        params['delay'] = params.get('delay') or 0.0
        params['cooling_delay'] = params.get('cooling_delay') or 0.0
    else:
        numeric_keys.extend(['delay', 'cooling_delay'])
    if excitation_type == 'current':
        numeric_keys.append('V_limit')
    else:
        numeric_keys.append('I_limit')

    have_all_numeric = all(params[k] is not None for k in numeric_keys)
    saved_matches_excitation = bool(saved) and saved.get('excitation_type') == excitation_type

    if not have_all_numeric:
        if saved_matches_excitation and not args.yes and not loaded:
            print("\nНайдены сохранённые параметры:")
            print(f"  Возбуждение ({unit}): {saved.get('X_start')} → {saved.get('X_stop')}, шаг {saved.get('X_step')} {unit}")
            if excitation_type == 'current':
                print(f"  Ограничение напряжения: {saved.get('V_limit')} В")
            else:
                print(f"  Ограничение тока: {saved.get('I_limit')} А")
            print(f"  Задержка на установку: {saved.get('delay')} с")
            print(f"  Задержка на охлаждение: {saved.get('cooling_delay')} с")
            print(f"  Последний комментарий: {saved.get('label', '')}")
            use_prev = input("\nИспользовать эти параметры? (y/n, по умолчанию y): ").strip().lower()
            if use_prev != 'n':
                for k in numeric_keys:
                    if params[k] is None:
                        params[k] = saved.get(k)

        # Если всё ещё чего-то не хватает — спрашиваем интерактивно
        if not all(params[k] is not None for k in numeric_keys):
            print("\n=== Настройка измерения ===")
            # Планировщик кастомных программ (BETA) заменяет X_start/X_stop/
            # X_step текстом программы — эти три поля не спрашиваются вовсе.
            if params.get('custom_program'):
                if not params['custom_program']:
                    while True:
                        text = input(
                            "Кастомная программа (например '-25, 0:40:10, -15, +5'): "
                        ).strip()
                        try:
                            from sweep import parse_custom_program
                            parse_custom_program(text)
                        except ValueError as e:
                            print(f"Ошибка: {e}")
                            continue
                        params['custom_program'] = text
                        break
            else:
                if params['X_start'] is None:
                    params['X_start'] = _prompt_float(f"Начальное значение возбуждения ({unit}): ")
                if params['X_stop'] is None:
                    params['X_stop'] = _prompt_float(
                        f"Конечное значение возбуждения ({unit}): ",
                        validator=lambda v: v >= params['X_start'],
                        error_msg=f"Конечное значение должно быть не меньше начального ({params['X_start']} {unit}).",
                    )
                if params['X_step'] is None:
                    params['X_step'] = _prompt_float(
                        f"Шаг возбуждения ({unit}): ",
                        validator=lambda v: v > 0,
                        error_msg="Шаг возбуждения должен быть положительным числом.",
                    )
            if excitation_type == 'current' and params['V_limit'] is None:
                params['V_limit'] = _prompt_float(
                    "Ограничение напряжения на источнике (В): ",
                    validator=lambda v: v > 0,
                    error_msg="Ограничение напряжения должно быть положительным числом.",
                )
            if excitation_type == 'voltage' and params['I_limit'] is None:
                params['I_limit'] = _prompt_float(
                    "Ограничение тока на источнике (А): ",
                    validator=lambda v: v > 0,
                    error_msg="Ограничение тока должно быть положительным числом.",
                )
            # Плавное нарастание (BETA) взаимно исключает delay/cooling_delay
            # (см. measurement.run_measurement) — не спрашиваем их вовсе,
            # спрашиваем время шага вместо них.
            if params.get('smooth_ramp'):
                params['delay'] = 0.0
                params['cooling_delay'] = 0.0
                if params.get('ramp_duration') is None:
                    params['ramp_duration'] = _prompt_float(
                        "Время перехода между точками при плавном нарастании (с): ",
                        validator=lambda v: v > 0,
                        error_msg="Время перехода должно быть положительным числом.",
                    )
            else:
                if params['delay'] is None:
                    params['delay'] = _prompt_float(
                        "Задержка на установку возбуждения (с): ",
                        validator=lambda v: v >= 0,
                        error_msg="Задержка не может быть отрицательной.",
                    )
                if params['cooling_delay'] is None:
                    params['cooling_delay'] = _prompt_float(
                        "Задержка на охлаждение между точками (с): ",
                        validator=lambda v: v >= 0,
                        error_msg="Задержка не может быть отрицательной.",
                    )

    # I_nom и ratio — необязательные метаданные датчика: без них измерение
    # проводится как обычно, они лишь попадают в шапку CSV. Спрашивать их
    # безусловно нельзя: measure тогда лезет в input() при любом запуске и
    # виснет на stdin в скриптах, CI и под --yes. Единственный случай, когда
    # ratio действительно необходим — включённая отсечка по погрешности
    # (по нему считается ожидаемый выход датчика).
    if params['stop_on_error'] and params['ratio'] is None:
        params['ratio'] = _prompt_float(
            "Коэффициент преобразования 1:X (нужен для отсечки по погрешности; передайте X): ",
            validator=lambda v: v > 0,
            error_msg="Коэффициент должен быть положительным числом.",
        )

    # --- label ---
    if params['label'] is None:
        last_label = saved.get('label', '') if saved else ''
        hint = f" (Enter для '{last_label}')" if last_label else ""
        label = input(f"Комментарий (датчик, пометка){hint}: ").strip()
        params['label'] = label if label else last_label

    # Финальная проверка
    errors = validate_measure_params(params, excitation_type)
    if errors:
        raise ValueError("Некорректные параметры измерения:\n  " + "\n  ".join(errors))

    # Предупреждение (не запрет) о работе свыше паспортного тока реле.
    # Печатается даже под --yes: это техника безопасности, а не вопрос, на
    # который можно молча ответить "да" за оператора. Единственное, что его
    # гасит, — явный --suppress-warnings (п.38), а не обычный --yes.
    if not params['suppress_notifications'] and params.get('branch') != Branch.NO_RELAY.value:
        warning = relay_current_warning(current_sweep_max_abs(params, excitation_type))
        if warning:
            print(f"\n⚠ {warning}\n")

    # Сохраняем конфиг датчика, если указано
    if args.save_config and sensor_config_mgr:
        try:
            sensor_config_mgr.save_sensor_config(args.save_config, params)
            print(f"Конфиг датчика сохранён как '{args.save_config}'.")
        except ValueError as e:
            print(f"Не удалось сохранить конфиг датчика: {e}")

    # Сохраняем итоговые параметры для следующего запуска
    config_mgr.save(params)

    return params


def make_csv_filename(data_dir: Path, label: str) -> Path:
    label_safe = re.sub(r'[^a-zA-Z0-9_\- ]', '', label).replace(' ', '_') if label else 'nolabel'
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return data_dir / f"IVtrace_{label_safe}_{timestamp_str}.csv"
