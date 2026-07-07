import argparse
import re
from datetime import datetime
from pathlib import Path

from config import ConfigManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="IVtrace — автоматизация снятия амплитудной характеристики датчиков тока.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path("data"),
        help="Каталог для хранения CSV/PNG и конфига (по умолчанию: ./data)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ---------------- measure ----------------
    p_measure = subparsers.add_parser("measure", help="Выполнить измерение амплитудной характеристики")
    p_measure.add_argument("--start", type=float, help="Начальный ток, А")
    p_measure.add_argument("--stop", type=float, help="Конечный ток, А")
    p_measure.add_argument("--step", type=float, help="Шаг по току, А")
    p_measure.add_argument("--vlimit", type=float, help="Ограничение напряжения на источнике, В")
    p_measure.add_argument("--delay", type=float, help="Задержка на установку тока, с")
    p_measure.add_argument("--cool", type=float, help="Задержка на охлаждение между точками, с")
    p_measure.add_argument("--direction", choices=["positive", "negative"], help="Ветвь измерения")
    p_measure.add_argument("--label", type=str, help="Комментарий (датчик, пометка)")
    p_measure.add_argument(
        "--dmm-addr", type=str, default=None,
        help="VISA-адрес мультиметра (пропустить автоопределение)",
    )
    p_measure.add_argument(
        "--src-addr", type=str, default=None,
        help="VISA-адрес источника тока (пропустить автоопределение)",
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

def _prompt_float(prompt: str) -> float:
    while True:
        try:
            return float(input(prompt))
        except ValueError:
            print("Ошибка ввода: введите число. Попробуйте снова.")


def resolve_measure_params(args, config_mgr: ConfigManager) -> dict:
    """
    Заполняет параметры измерения: сперва из аргументов командной строки,
    затем (если чего-то не хватает) — из сохранённого конфига или интерактивного ввода.
    Обновляет конфиг сохранёнными значениями.
    """
    saved = config_mgr.load()

    params = {
        'I_start': args.start,
        'I_stop': args.stop,
        'I_step': args.step,
        'V_limit': args.vlimit,
        'delay': args.delay,
        'cooling_delay': args.cool,
        'direction': args.direction,
        'label': args.label,
    }

    numeric_keys = ['I_start', 'I_stop', 'I_step', 'V_limit', 'delay', 'cooling_delay']
    have_all_numeric = all(params[k] is not None for k in numeric_keys)

    if not have_all_numeric:
        if saved and not args.yes:
            print("\nНайдены сохранённые параметры:")
            print(f"  Ток: {saved.get('I_start')} → {saved.get('I_stop')} А, шаг {saved.get('I_step')} А")
            print(f"  Ограничение напряжения: {saved.get('V_limit')} В")
            print(f"  Задержка на установку: {saved.get('delay')} с")
            print(f"  Задержка на охлаждение: {saved.get('cooling_delay')} с")
            print(f"  Последняя ветвь: {saved.get('direction', '?')}")
            print(f"  Последний комментарий: {saved.get('label', '')}")
            use_prev = input("\nИспользовать эти параметры? (y/n, по умолчанию y): ").strip().lower()
            if use_prev != 'n':
                for k in numeric_keys:
                    if params[k] is None:
                        params[k] = saved.get(k)

        # Если всё ещё чего-то не хватает — спрашиваем интерактивно
        if not all(params[k] is not None for k in numeric_keys):
            print("\n=== Настройка измерения ===")
            if params['I_start'] is None:
                params['I_start'] = _prompt_float("Начальный ток (А): ")
            if params['I_stop'] is None:
                params['I_stop'] = _prompt_float("Конечный ток (А): ")
            if params['I_step'] is None:
                params['I_step'] = _prompt_float("Шаг по току (А): ")
            if params['V_limit'] is None:
                params['V_limit'] = _prompt_float("Ограничение напряжения на источнике (В): ")
            if params['delay'] is None:
                params['delay'] = _prompt_float("Задержка на установку тока (с): ")
            if params['cooling_delay'] is None:
                params['cooling_delay'] = _prompt_float("Задержка на охлаждение между точками (с): ")

    # --- label ---
    if params['label'] is None:
        last_label = saved.get('label', '') if saved else ''
        hint = f" (Enter для '{last_label}')" if last_label else ""
        label = input(f"Комментарий (датчик, пометка){hint}: ").strip()
        params['label'] = label if label else last_label

    # --- direction ---
    if params['direction'] is None:
        last_dir = saved.get('direction', '') if saved else ''
        hint = f" (Enter для {last_dir}, или введите p/n/+/-)" if last_dir else ""
        while True:
            dir_input = input(f"Ветвь (positive/p/+ или negative/n/-){hint}: ").strip().lower()
            if dir_input == '' and last_dir:
                dir_input = last_dir
            if dir_input in ('positive', 'p', '+'):
                params['direction'] = 'positive'
                break
            elif dir_input in ('negative', 'n', '-'):
                params['direction'] = 'negative'
                break
            else:
                print("Некорректная ветвь. Используйте positive/p/+ или negative/n/-")

    # Сохраняем итоговые параметры для следующего запуска
    config_mgr.save(params)

    return params


def make_csv_filename(data_dir: Path, label: str, direction: str) -> Path:
    label_safe = re.sub(r'[^a-zA-Z0-9_\- ]', '', label).replace(' ', '_') if label else 'nolabel'
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    return data_dir / f"IVtrace_{label_safe}_{direction}_{timestamp_str}.csv"
