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
)
from measurement import (
    DEFAULT_AVERAGING_COUNT, DEFAULT_AVERAGING_DELAY, DEFAULT_DISCARD_FIRST,
    DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER,
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

    # ---------------- measure ----------------
    p_measure = subparsers.add_parser(
        "measure",
        help="Выполнить измерение амплитудной характеристики (обе полярности через реле)",
    )
    p_measure.add_argument(
        "--excitation", choices=["current", "voltage"], default=None,
        help="Тип возбуждения датчика: current (источник тока) или voltage (источник напряжения)",
    )
    p_measure.add_argument("--start", type=float, help="Начальное значение возбуждения (обычно 0)")
    p_measure.add_argument("--stop", type=float, help="Конечное значение возбуждения")
    p_measure.add_argument("--step", type=float, help="Шаг возбуждения")
    p_measure.add_argument("--vlimit", type=float, help="Ограничение напряжения на источнике тока, В (не используется для источника напряжения)")
    p_measure.add_argument("--delay", type=float, help="Задержка на установку возбуждения, с")
    p_measure.add_argument("--cool", type=float, help="Задержка на охлаждение между точками, с")
    p_measure.add_argument("--label", type=str, help="Комментарий (датчик, пометка)")
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
        "--error-threshold", type=float, default=None,
        help="Порог погрешности для досрочной остановки, % (по умолчанию 1.0)",
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
        "--adaptive-cooling-max-multiplier", type=float, default=None,
        help="Потолок роста задержки охлаждения при --adaptive-cooling, "
             f"во сколько раз от базовой на максимуме развёртки (по умолчанию "
             f"{DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER:.0f})",
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
    p_analyze = subparsers.add_parser("analyze", help="Построить график и рассчитать погрешность по последнему CSV")
    p_analyze.add_argument("--file", type=Path, default=None, help="Путь к конкретному CSV (по умолчанию — последний в data-dir)")
    p_analyze.add_argument("--inom", type=float, default=None, help="Номинальный первичный ток датчика, А")
    p_analyze.add_argument("--ratio", type=float, default=None, help="Коэффициент преобразования 1:X (передать X)")
    p_analyze.add_argument("--no-show", action="store_true", help="Не открывать окно с графиком (только сохранить PNG)")

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
    """
    if excitation_type != 'current':
        return None
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
    if params.get('X_step') is None or params['X_step'] <= 0:
        errors.append("Шаг возбуждения должен быть положительным числом.")
    if params.get('X_start') is None or params.get('X_stop') is None:
        errors.append("Начальное и конечное значения должны быть заданы.")
    # X_stop < X_start больше НЕ ошибка (п.17): планировщик (sweep.py)
    # поддерживает любой знак и порядок X_start/X_stop — 250→0 (по модулю
    # убывающий проход) не менее корректен, чем 0→250. Порядок точек внутри
    # развёртки определяет sweep.plan_sweep(), не эта проверка.
    if params.get('turns') is not None and params['turns'] <= 0:
        errors.append("Число витков должно быть положительным числом.")
    if params.get('delay') is not None and params['delay'] < 0:
        errors.append("Задержка на установку не может быть отрицательной.")
    if params.get('cooling_delay') is not None and params['cooling_delay'] < 0:
        errors.append("Задержка на охлаждение не может быть отрицательной.")
    if excitation_type == 'current' and (params.get('V_limit') is None or params['V_limit'] <= 0):
        errors.append("Ограничение напряжения должно быть положительным числом.")

    if excitation_type == 'current':
        block = relay_current_block_reason(current_sweep_max_abs(params, excitation_type))
        if block:
            errors.append(block)

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
        'X_start': args.start if args.start is not None else loaded.get('X_start'),
        'X_stop': args.stop if args.stop is not None else loaded.get('X_stop'),
        'X_step': args.step if args.step is not None else loaded.get('X_step'),
        'V_limit': args.vlimit if args.vlimit is not None else loaded.get('V_limit'),
        'delay': args.delay if args.delay is not None else loaded.get('delay'),
        'cooling_delay': args.cool if args.cool is not None else loaded.get('cooling_delay'),
        'label': args.label if args.label is not None else loaded.get('label'),
        # Новые параметры
        'I_nom': args.inom if args.inom is not None else loaded.get('I_nom'),
        'ratio': args.ratio if args.ratio is not None else loaded.get('ratio'),
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
        'adaptive_cooling_max_multiplier': (
            args.adaptive_cooling_max_multiplier if args.adaptive_cooling_max_multiplier is not None
            else loaded.get('adaptive_cooling_max_multiplier', DEFAULT_ADAPTIVE_COOLING_MAX_MULTIPLIER)
        ),
    }

    # Для источника напряжения V_limit не используется
    if excitation_type == 'voltage':
        params['V_limit'] = 0.0

    numeric_keys = ['X_start', 'X_stop', 'X_step', 'delay', 'cooling_delay']
    if excitation_type == 'current':
        numeric_keys.append('V_limit')

    have_all_numeric = all(params[k] is not None for k in numeric_keys)
    saved_matches_excitation = bool(saved) and saved.get('excitation_type') == excitation_type

    if not have_all_numeric:
        if saved_matches_excitation and not args.yes and not loaded:
            print("\nНайдены сохранённые параметры:")
            print(f"  Возбуждение ({unit}): {saved.get('X_start')} → {saved.get('X_stop')}, шаг {saved.get('X_step')} {unit}")
            if excitation_type == 'current':
                print(f"  Ограничение напряжения: {saved.get('V_limit')} В")
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
    # Печатается безусловно, даже под --yes: это техника безопасности, а не
    # вопрос, на который можно молча ответить "да" за оператора.
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
