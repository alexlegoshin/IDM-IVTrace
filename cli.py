import argparse
import re
from datetime import datetime
from pathlib import Path

from config import ConfigManager


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
    # required=False: без подкоманды запускается GUI (см. run.main).
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

def validate_measure_params(params: dict, excitation_type: str) -> list:
    """
    Проверяет числовые параметры измерения. Возвращает список текстовых
    описаний ошибок (пустой — если всё в порядке). Используется и в CLI
    (resolve_measure_params), и в GUI, чтобы правила были едиными.

    Защищает от X_step<=0 (деление на ноль в measurement.py) и
    X_stop < X_start (пустой проход измерения), а также от отрицательных
    задержек и неположительного V_limit для источника тока.
    """
    errors = []
    if params.get('X_step') is None or params['X_step'] <= 0:
        errors.append("Шаг возбуждения должен быть положительным числом.")
    if params.get('X_start') is None or params.get('X_stop') is None or params['X_stop'] < params['X_start']:
        errors.append("Конечное значение должно быть не меньше начального.")
    if params.get('delay') is not None and params['delay'] < 0:
        errors.append("Задержка на установку не может быть отрицательной.")
    if params.get('cooling_delay') is not None and params['cooling_delay'] < 0:
        errors.append("Задержка на охлаждение не может быть отрицательной.")
    if excitation_type == 'current' and (params.get('V_limit') is None or params['V_limit'] <= 0):
        errors.append("Ограничение напряжения должно быть положительным числом.")
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


def resolve_measure_params(args, config_mgr: ConfigManager) -> dict:
    """
    Заполняет параметры измерения: сперва из аргументов командной строки,
    затем (если чего-то не хватает) — из сохранённого конфига или интерактивного ввода.
    Обновляет конфиг сохранёнными значениями.

    Тип возбуждения (ток/напряжение) запрашивается в первую очередь, так как
    от него зависит, в какой папке искать конфиг источника (instruments/
    current_sources или instruments/voltage_sources) и какие единицы
    измерения использовать для X_start/X_stop/X_step.

    Направление (ветвь) не запрашивается: плата реле сама выполняет проход
    в обе стороны (forward + reverse) в рамках одного запуска measure.
    """
    saved = config_mgr.load()

    # --- excitation type — спрашиваем в первую очередь ---
    excitation_type = args.excitation
    if excitation_type is None:
        last_excitation = saved.get('excitation_type') if saved else None
        if last_excitation and args.yes:
            # --yes означает "не спрашивать, использовать сохранённое, если есть"
            excitation_type = last_excitation
        elif last_excitation:
            hint = 'ток' if last_excitation == 'current' else 'напряжение'
            use_prev = input(f"Последний раз использовалось возбуждение: {hint}. Использовать снова? (y/n, по умолчанию y): ").strip().lower()
            if use_prev != 'n':
                excitation_type = last_excitation
        if excitation_type is None:
            excitation_type = _prompt_excitation_type()

    unit = 'А' if excitation_type == 'current' else 'В'

    params = {
        'excitation_type': excitation_type,
        'X_start': args.start,
        'X_stop': args.stop,
        'X_step': args.step,
        'V_limit': args.vlimit,
        'delay': args.delay,
        'cooling_delay': args.cool,
        'label': args.label,
    }

    # V_limit нужен только для источника тока (ограничение по напряжению).
    # Для источника напряжения он не используется вовсе (см. measurement.py).
    numeric_keys = ['X_start', 'X_stop', 'X_step', 'delay', 'cooling_delay']
    if excitation_type == 'current':
        numeric_keys.append('V_limit')
    else:
        params['V_limit'] = params['V_limit'] or 0.0  # не используется, но поле оставляем для совместимости CSV

    have_all_numeric = all(params[k] is not None for k in numeric_keys)

    # Подсказки из сохранённого конфига валидны только если тип возбуждения совпадает
    saved_matches_excitation = bool(saved) and saved.get('excitation_type') == excitation_type

    if not have_all_numeric:
        if saved_matches_excitation and not args.yes:
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

    # Финальная проверка — покрывает и значения из --флагов/сохранённого
    # конфига (не проходившие через интерактивные валидаторы выше), и
    # защищает от X_step=0 (деление на ноль в measurement.py) и
    # X_stop < X_start (пустой проход измерения).
    errors = validate_measure_params(params, excitation_type)
    if errors:
        raise ValueError("Некорректные параметры измерения:\n  " + "\n  ".join(errors))

    # --- label ---
    if params['label'] is None:
        last_label = saved.get('label', '') if saved else ''
        hint = f" (Enter для '{last_label}')" if last_label else ""
        label = input(f"Комментарий (датчик, пометка){hint}: ").strip()
        params['label'] = label if label else last_label

    # Сохраняем итоговые параметры для следующего запуска
    config_mgr.save(params)

    return params


def make_csv_filename(data_dir: Path, label: str) -> Path:
    """
    Имя файла не содержит ветвь (positive/negative) — один CSV теперь
    содержит обе полярности, а различие фиксируется в колонке Branch.
    """
    label_safe = re.sub(r'[^a-zA-Z0-9_\- ]', '', label).replace(' ', '_') if label else 'nolabel'
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return data_dir / f"IVtrace_{label_safe}_{timestamp_str}.csv"
