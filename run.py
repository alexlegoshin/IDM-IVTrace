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
from pathlib import Path

from apppaths import default_data_dir, sensor_config_dir, work_dir, set_work_dir
from cli import build_parser, resolve_measure_params, make_csv_filename
from config import ConfigManager, SensorConfigManager
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
    from applog import get_logger
    flog = get_logger(__name__)

    lines = []

    visa = check_visa()
    lines.append("[VISA] " + visa.summary_line())
    flog.info("Предполёт: VISA — %s", visa.summary_line())
    if not visa.ok:
        lines.append(visa.message)
        flog.error("Предполёт заблокирован: NI-VISA недоступна")
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
    # Без этого --save-config/--load-config тихо ничего не делали (см.
    # cli.resolve_measure_params: "if args.load_config and sensor_config_mgr"
    # — без менеджера условие всегда ложно).
    sensor_config_mgr = SensorConfigManager(sensor_config_dir())

    try:
        params = resolve_measure_params(args, config_mgr, sensor_config_mgr)
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
    else:
        print(f"Ограничение тока источника: {params['I_limit']} А")
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

    # Автопостроение графика по окончании измерения (п.22), не ломая ручной
    # режим (analyze, п.20) — та же analysis.load_and_analyze. Без I ном./
    # коэффициента сравнивать не с чем, тогда просто пропускаем без ошибки.
    from analysis import load_and_analyze_from_params
    stats = load_and_analyze_from_params(csv_path, params, show=False, close_fig=True)
    if stats is not None:
        print(f"\nГрафик сохранён: {stats['png_path']}")
        print(f"Максимальная приведённая погрешность: {stats['max_error_percent']:+.4f} %")
        print(f"Средняя приведённая погрешность:   {stats['mean_error_percent']:+.4f} %")
        try:
            import os
            if hasattr(os, 'startfile'):
                os.startfile(stats['png_path'])
        except OSError:
            pass

    return 0


def cmd_analyze(args) -> int:
    data_dir = args.data_dir

    csv_path = args.file if args.file else find_latest_csv(data_dir)
    print(f"Файл: {csv_path}")

    if args.estimate_ratio:
        from analysis import estimate_ratio_from_data, metadata_zero_offset
        import pandas as pd
        df = pd.read_csv(csv_path, comment='#')
        zero_offset = args.zero_offset if args.zero_offset is not None else (metadata_zero_offset(csv_path) or 0.0)
        try:
            result = estimate_ratio_from_data(df, zero_offset=zero_offset)
        except ValueError as e:
            print(f"Ошибка: {e}")
            return 1
        print("\nBETA: определение коэффициента преобразования по снятым точкам (МНК).")
        print(f"Фактический коэффициент: 1:{result['X_actual']:.2f}")
        print(f"Округлённый (кратно 25): 1:{result['X_rounded']:.0f} "
              f"(расхождение {result['discrepancy_percent']:.2f}%)")
        return 0

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

    stats = load_and_analyze(csv_path, I_nom=I_nom, X=X, show=not args.no_show,
                             show_error_labels=args.labels, zero_offset=args.zero_offset)

    print(f"\nГрафик сохранён: {stats['png_path']}")
    print(f"Максимальная приведённая погрешность: {stats['max_error_percent']:+.4f} %")
    print(f"Средняя приведённая погрешность:   {stats['mean_error_percent']:+.4f} %")
    if stats['rejected_points']:
        print(f"Исключено из статистики: {stats['rejected_points']} из {stats['points']} точек")

    if args.xlsx:
        from analysis import export_xlsx
        xlsx_path = export_xlsx(csv_path)
        print(f"XLSX сохранён: {xlsx_path}")

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


def cmd_discover(args) -> int:
    """Разовый поиск приборов и платы реле (Ф4, п.25) — диагностика без запуска измерения."""
    from apppaths import (
        multimeter_cfg_dir, voltmeter_cfg_dir, current_source_cfg_dir, voltage_source_cfg_dir,
    )
    from discovery import scan_instruments
    from relay import discover_relay_port

    try:
        from visa_backend import make_resource_manager
        rm = make_resource_manager()
    except RuntimeError as e:
        print(f"Ошибка VISA: {e}")
        return 1

    print("=== Поиск приборов ===")
    found = scan_instruments(rm, {
        'multimeter': multimeter_cfg_dir(),
        'voltmeter': voltmeter_cfg_dir(),
        'current_source': current_source_cfg_dir(),
        'voltage_source': voltage_source_cfg_dir(),
    })
    try:
        rm.close()
    except Exception:
        pass

    if not found:
        print("Ничего не найдено.")
    for instr in found:
        model = instr.config_path.stem if instr.config_path else "не опознан"
        print(f"  {instr.address}  [{instr.kind}]  {instr.idn}  ->  {model}")

    print("\n=== Поиск платы реле ===")
    try:
        port = discover_relay_port()
        print(f"Плата реле найдена на {port}")
    except RuntimeError as e:
        print(f"{e}")

    return 0


def cmd_relay(args) -> int:
    """Ручное управление платой реле напрямую, вне измерительного цикла (Ф4, п.13)."""
    from relay import RelayController, discover_relay_port

    label = {"forward": "прямое направление (IFW)", "reverse": "обратное направление (IRW)",
             "off": "разомкнуть (I_0)"}[args.direction]
    if not args.yes:
        confirm = input(f"Переключить реле в положение: {label}? (y/n): ").strip().lower()
        if confirm != 'y':
            print("Отменено.")
            return 1

    port = args.relay_port
    if not port:
        try:
            port = discover_relay_port()
        except RuntimeError as e:
            print(f"Ошибка: {e}")
            return 1

    relay = RelayController(port)
    try:
        if args.direction == "forward":
            resp = relay.forward()
        elif args.direction == "reverse":
            resp = relay.reverse()
        else:
            resp = relay.off()
        print(f"Реле ({port}): {label} -> {resp}")
    finally:
        relay.close()

    return 0


def cmd_setpoint(args) -> int:
    """Прямая знаковая уставка вне измерительного цикла (Ф5, п.40) — держит значение до Enter/Ctrl+C."""
    from limits import relay_current_block_reason, relay_current_warning

    if args.excitation == 'current':
        block = relay_current_block_reason(abs(args.value))
        if block:
            print(f"Ошибка: {block}")
            return 1
        warning = relay_current_warning(abs(args.value))
        if warning and not args.yes:
            confirm = input(f"⚠ {warning}\nПродолжить? (y/n): ").strip().lower()
            if confirm != 'y':
                print("Отменено.")
                return 1
        if args.vlimit is None:
            print("Ошибка: для --excitation current нужно указать --vlimit.")
            return 1

    if args.excitation == 'voltage' and args.ilimit is None:
        print("Ошибка: для --excitation voltage нужно указать --ilimit.")
        return 1

    if not args.yes:
        confirm = input(f"Установить уставку {args.value:+g} ({args.excitation})? (y/n): ").strip().lower()
        if confirm != 'y':
            print("Отменено.")
            return 1

    try:
        from visa_backend import make_resource_manager
        rm = make_resource_manager()
    except RuntimeError as e:
        print(f"Ошибка VISA: {e}")
        return 1

    from orchestrate import open_manual_control_session
    try:
        session = open_manual_control_session(
            rm, args.excitation, V_limit=args.vlimit, I_limit=args.ilimit,
            dmm_addr=args.dmm_addr, src_addr=args.src_addr, relay_port=args.relay_port,
        )
    except (RuntimeError, ValueError) as e:
        print(f"Ошибка: {e}")
        try:
            rm.close()
        except Exception:
            pass
        return 1

    try:
        session.apply_setpoint(args.value)
        print(f"Уставка применена: {args.value:+g}. Нажмите Enter для остановки...")
        try:
            input()
        except KeyboardInterrupt:
            print()
    finally:
        session.stop()
        session.close()
        try:
            rm.close()
        except Exception:
            pass

    print("Остановлено.")
    return 0


def cmd_identify(args) -> int:
    """«Мигнуть» прибором по VISA-адресу (Ф4, п.11 — CLI-паритет, Ф5 п.34)."""
    import json
    from apppaths import (
        multimeter_cfg_dir, voltmeter_cfg_dir, current_source_cfg_dir, voltage_source_cfg_dir,
    )
    from discovery import scan_instruments
    from instruments import identify_instrument

    try:
        from visa_backend import make_resource_manager
        rm = make_resource_manager()
    except RuntimeError as e:
        print(f"Ошибка VISA: {e}")
        return 1

    found = scan_instruments(rm, {
        'multimeter': multimeter_cfg_dir(), 'voltmeter': voltmeter_cfg_dir(),
        'current_source': current_source_cfg_dir(), 'voltage_source': voltage_source_cfg_dir(),
    })
    match = next((i for i in found if i.address == args.address), None)
    try:
        rm.close()
    except Exception:
        pass

    if match is None or match.config_path is None:
        print(f"Прибор по адресу {args.address} не опознан сканом — нечем определить конфиг с командой мигания.")
        return 1

    # Новый ResourceManager: предыдущий уже закрыт после скана — не держим
    # его открытым дольше, чем нужно для самого перебора ресурсов.
    rm = make_resource_manager()
    cfg = json.loads(match.config_path.read_text(encoding='utf-8'))
    ok = identify_instrument(rm, args.address, cfg)
    try:
        rm.close()
    except Exception:
        pass

    if ok:
        print(f"Команда отправлена: {args.address} ({match.config_path.stem})")
        return 0
    print(f"Для {match.config_path.stem} не настроена команда мигания (identify_command в конфиге отсутствует).")
    return 1


def cmd_profile(args) -> int:
    """Профили датчиков (п.39) — CLI-паритет к сохранению/загрузке из GUI."""
    from config import SensorConfigManager

    mgr = SensorConfigManager(sensor_config_dir())

    if args.profile_command == "list":
        names = mgr.list_sensor_configs(excitation_type=args.excitation)
        if not names:
            print("Профилей не найдено.")
        for name in names:
            print(f"  {name}")
        return 0

    if args.profile_command == "delete":
        if not args.yes:
            confirm = input(f"Удалить профиль '{args.name}'? (y/n): ").strip().lower()
            if confirm != 'y':
                print("Отменено.")
                return 1
        ok = mgr.delete_sensor_config(args.name, excitation_type=args.excitation)
        print("Удалён." if ok else "Профиль не найден.")
        return 0 if ok else 1

    if args.profile_command == "rename":
        ok = mgr.rename_sensor_config(args.old_name, args.new_name, excitation_type=args.excitation)
        print("Переименован." if ok else "Исходный профиль не найден.")
        return 0 if ok else 1

    return 1


def cmd_calibration(args) -> int:
    """Поверка приборов — реестр физических экземпляров (п.3-UI, бага 6/7 — CLI-паритет)."""
    from apppaths import (
        multimeter_cfg_dir, voltmeter_cfg_dir, current_source_cfg_dir, voltage_source_cfg_dir,
    )
    from calibration import (
        known_models, list_calibration_rows, set_calibration_record, delete_calibration_record,
    )

    config_dirs = [multimeter_cfg_dir(), voltmeter_cfg_dir(), current_source_cfg_dir(), voltage_source_cfg_dir()]

    if args.calibration_command == "list":
        rows = list_calibration_rows(config_dirs)
        if not rows:
            print("Конфигов приборов не найдено.")
        for row in rows:
            info = row['info']
            serial = row['serial_number'] or '—'
            print(f"  {row['model_id']}  S/N {serial}  [{info.status.value}]  {info.message}")
        return 0

    if args.calibration_command == "set":
        models = known_models(config_dirs)
        if args.model_id not in models:
            print(f"Модель {args.model_id!r} не найдена ни в одном из каталогов приборов "
                  "(см. calibration list).")
            return 1
        try:
            set_calibration_record(args.model_id, args.serial, args.date, args.interval_months,
                                    comment=args.comment)
        except ValueError as e:
            print(f"Ошибка: {e}")
            return 1
        print(f"Записано: {args.model_id} (S/N {args.serial or '—'})")
        return 0

    if args.calibration_command == "delete":
        delete_calibration_record(args.model_id, args.serial)
        print(f"Удалено (если существовало): {args.model_id} (S/N {args.serial or '—'})")
        return 0

    return 1


def cmd_config(args) -> int:
    """Настройки приложения (п.23 — рабочая папка)."""
    if args.config_command == "show":
        current = work_dir()
        print(f"Рабочая папка: {current}")
        print("(значение по умолчанию)" if current == default_data_dir() else "(переопределена)")
        return 0

    if args.config_command == "set-work-dir":
        set_work_dir(args.path)
        print(f"Рабочая папка установлена: {args.path}")
        return 0

    if args.config_command == "reset-work-dir":
        set_work_dir(None)
        print(f"Рабочая папка сброшена на значение по умолчанию: {default_data_dir()}")
        return 0

    return 1


def main(argv=None) -> int:
    # work_dir() (п.23) — уважает переопределение рабочей папки, заданное
    # из GUI/через `config set-work-dir`; без него — то же самое, что и
    # раньше (default_data_dir()).
    parser = build_parser(default_data_dir=work_dir())
    args = parser.parse_args(argv)

    # Файловое логирование (п.15) — поднимаем до любой работы с железом/файлами,
    # чтобы в лог попал и сам старт. component — по подкоманде (gui/measure/...),
    # чтобы в общем файле было видно, что именно запускалось.
    from applog import setup_logging, get_logger
    setup_logging(args.command or "gui")
    get_logger(__name__).info("CLI/GUI старт: команда=%s", args.command or "gui")

    # Без подкоманды или с 'gui' — запускаем графический интерфейс.
    if args.command is None or args.command == "gui":
        return cmd_gui(args)
    if args.command == "measure":
        return cmd_measure(args)
    if args.command == "analyze":
        return cmd_analyze(args)
    if args.command == "selftest":
        return cmd_selftest(args)
    if args.command == "discover":
        return cmd_discover(args)
    if args.command == "relay":
        return cmd_relay(args)
    if args.command == "setpoint":
        return cmd_setpoint(args)
    if args.command == "identify":
        return cmd_identify(args)
    if args.command == "profile":
        return cmd_profile(args)
    if args.command == "calibration":
        return cmd_calibration(args)
    if args.command == "config":
        return cmd_config(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
