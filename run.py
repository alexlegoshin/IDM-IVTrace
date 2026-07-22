#!/usr/bin/env python
"""
IVtrace — приложение для автоматизированного снятия амплитудной
характеристики датчиков тока/напряжения. Работает и как CLI, и как GUI.

Запуск GUI (по умолчанию, без аргументов или подкомандой gui):
    python run.py
    python run.py gui

Измерение из CLI:
    python run.py measure --excitation current --start 0 --stop 10 --step 0.5 --vlimit 5 \
        --delay 0.2 --cool 0.5 --label "Sensor1"

    python run.py measure --excitation voltage --start 0 --stop 64 --step 5 \
        --delay 1 --cool 0.5 --label "VoltageSensor1"

Анализ:
    python run.py analyze --inom 150 --ratio 1500

Измерение автоматически проходит обе полярности (forward/reverse) за один
запуск — переключение направления делает плата реле.

Перед реальной работой с железом (measure/GUI) выполняется предполётная
проверка: (1) доступность NI-VISA и (2) виртуальные самотесты кода. При
провале любой из проверок измерение не запускается — это страховка
оборудования от повреждения из-за поломки кода.
"""
import sys

from apppaths import default_data_dir
from cli import build_parser, resolve_measure_params, make_csv_filename
from config import ConfigManager
from analysis import load_and_analyze, find_latest_csv


def preflight(skip_selftest: bool = False) -> tuple:
    """
    Предполётная проверка перед работой с железом.

    Возвращает (ok: bool, report: str). Проверяет:
      1. NI-VISA доступна и рабочая (visa_backend.check_visa);
      2. виртуальные самотесты кода проходят (selftest.run_selftests),
         если не отключены флагом.
    """
    from visa_backend import check_visa

    lines = []

    visa = check_visa()
    lines.append("[VISA] " + visa.summary_line())
    if not visa.ok:
        lines.append(visa.message)
        return False, "\n".join(lines)

    if skip_selftest:
        lines.append("[Самотесты] ПРОПУЩЕНЫ (--skip-selftest).")
        return True, "\n".join(lines)

    from selftest import run_selftests
    print("Выполняю самотесты (виртуальная проверка кода)...")
    st = run_selftests()
    lines.append("[Самотесты] " + ("OK — " if st.ok else "ПРОВАЛ — ") + st.summary)
    if not st.ok:
        lines.append(st.output)
        return False, "\n".join(lines)

    return True, "\n".join(lines)


def cmd_measure(args) -> int:
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    config_mgr = ConfigManager(data_dir / "ivtrace_config.json")

    try:
        params = resolve_measure_params(args, config_mgr)
    except ValueError as e:
        print(f"Ошибка параметров: {e}")
        return 1

    from measurement import EXCITATION_UNITS
    excitation_type = params['excitation_type']
    unit = EXCITATION_UNITS[excitation_type]

    csv_path = make_csv_filename(data_dir, params['label'])
    print(f"\nФайл результатов: {csv_path}")
    print(f"Возбуждение: {excitation_type} ({unit}), диапазон {params['X_start']}..{params['X_stop']} {unit}, "
          f"шаг {params['X_step']} {unit} (обе полярности через реле)")
    if excitation_type == 'current':
        print(f"Ограничение напряжения источника: {params['V_limit']} В")
    print(f"Комментарий: {params['label']}")
    print(f"Задержка установки: {params['delay']} с, задержка охлаждения: {params['cooling_delay']} с\n")

    # --- Предполётная проверка (VISA + самотесты) ---
    ok, report = preflight(skip_selftest=args.skip_selftest)
    print(report)
    if not ok:
        print("\nПредполётная проверка не пройдена — измерение отменено.")
        return 1
    print()

    from visa_backend import make_resource_manager
    from orchestrate import run_measurement_session

    try:
        rm = make_resource_manager()
    except RuntimeError as e:
        print(f"Ошибка VISA: {e}")
        return 1

    try:
        df = run_measurement_session(
            rm, params, csv_path,
            dmm_addr=args.dmm_addr, src_addr=args.src_addr, relay_port=args.relay_port,
            log=print,
        )
    except RuntimeError as e:
        print(f"Ошибка измерения: {e}")
        return 1
    finally:
        try:
            rm.close()
        except Exception:
            pass

    print()
    print(df.head(10).to_string(index=False))
    return 0


def cmd_analyze(args) -> int:
    data_dir = args.data_dir

    csv_path = args.file if args.file else find_latest_csv(data_dir)
    print(f"Файл: {csv_path}")

    I_nom = args.inom
    if I_nom is None:
        while True:
            try:
                I_nom = float(input("Номинальный первичный ток датчика (А, напр. 150): "))
                if I_nom <= 0:
                    print("Ток должен быть положительным.")
                    continue
                break
            except ValueError:
                print("Введите число.")

    X = args.ratio
    if X is None:
        while True:
            try:
                X = float(input("Коэффициент преобразования 1:X, введите X (напр. 1500): "))
                if X <= 0:
                    print("X должен быть положительным.")
                    continue
                break
            except ValueError:
                print("Введите число.")

    stats = load_and_analyze(csv_path, I_nom=I_nom, X=X, show=not args.no_show)

    print(f"\nГрафик сохранён: {stats['png_path']}")
    print(f"Максимальная приведённая погрешность: {stats['max_error_percent']:.4f} %")
    print(f"Средняя приведённая погрешность:   {stats['mean_error_percent']:.4f} %")

    return 0


def cmd_gui(args) -> int:
    from gui import launch_gui
    return launch_gui(args)


def cmd_selftest(args) -> int:
    """Диагностика: проверка NI-VISA + прогон виртуальных самотестов."""
    from visa_backend import check_visa
    from selftest import run_selftests

    visa = check_visa()
    print("=== Проверка NI-VISA ===")
    print(visa.message)
    print()

    print("=== Виртуальные самотесты ===")
    st = run_selftests(verbose=True)
    print(st.output.rstrip())
    print()
    print(("ИТОГ: самотесты OK — " if st.ok else "ИТОГ: САМОТЕСТЫ ПРОВАЛЕНЫ — ") + st.summary)
    return 0 if st.ok else 1


def main(argv=None) -> int:
    parser = build_parser(default_data_dir=default_data_dir())
    args = parser.parse_args(argv)

    # Без подкоманды или с 'gui' — запускаем графический интерфейс.
    if args.command is None or args.command == "gui":
        return cmd_gui(args)
    if args.command == "measure":
        return cmd_measure(args)
    if args.command == "analyze":
        return cmd_analyze(args)
    if args.command == "selftest":
        return cmd_selftest(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
