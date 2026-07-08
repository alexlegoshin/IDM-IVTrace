#!/usr/bin/env python
"""
IVtrace — консольное приложение для автоматизированного снятия
амплитудной характеристики датчиков тока.

Использование:
    python run.py measure --start 0 --stop 10 --step 0.5 --vlimit 5 \
        --delay 0.2 --cool 0.5 --label "Sensor1"

    python run.py analyze --inom 150 --ratio 1500

Измерение теперь автоматически проходит обе полярности (forward/reverse)
за один запуск — переключение направления делает плата реле, вручную
задавать ветвь (positive/negative) больше не нужно.
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyvisa

from cli import build_parser, resolve_measure_params, make_csv_filename
from config import ConfigManager
from instruments import Multimeter, CurrentSource, discover_instruments
from relay import RelayController, discover_relay_port
from measurement import run_measurement
from analysis import load_and_analyze, find_latest_csv

BASE_DIR = Path(__file__).resolve().parent
MULTIMETER_CFG_DIR = BASE_DIR / "instruments" / "multimeters"
SOURCE_CFG_DIR = BASE_DIR / "instruments" / "standard_sources"


def cmd_measure(args) -> int:
    data_dir = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    config_mgr = ConfigManager(data_dir / "ivtrace_config.json")

    params = resolve_measure_params(args, config_mgr)

    csv_path = make_csv_filename(data_dir, params['label'])
    print(f"\nФайл результатов: {csv_path}")
    print(f"Диапазон: {params['I_start']}..{params['I_stop']} А, шаг {params['I_step']} А, "
          f"ограничение V={params['V_limit']} В (обе полярности через реле)")
    print(f"Комментарий: {params['label']}")
    print(f"Задержка установки: {params['delay']} с, задержка охлаждения: {params['cooling_delay']} с\n")

    rm = pyvisa.ResourceManager()

    try:
        if args.dmm_addr and args.src_addr:
            dmm_addr, src_addr = args.dmm_addr, args.src_addr
            # При ручном указании адресов всё равно нужно понять, какой конфиг использовать.
            # Опрашиваем *IDN? у каждого адреса, чтобы подобрать json.
            from instruments import find_config_for_idn
            dmm_instr = rm.open_resource(dmm_addr)
            dmm_instr.encoding = 'utf-8'
            dmm_idn = dmm_instr.query('*IDN?').strip()
            dmm_instr.close()
            dmm_cfg = find_config_for_idn(dmm_idn, MULTIMETER_CFG_DIR)
            if dmm_cfg is None:
                print(f"Не удалось подобрать конфиг мультиметра для IDN: {dmm_idn}")
                return 1

            src_instr = rm.open_resource(src_addr)
            src_instr.encoding = 'utf-8'
            src_idn = src_instr.query('*IDN?').strip()
            src_instr.close()
            src_cfg = find_config_for_idn(src_idn, SOURCE_CFG_DIR)
            if src_cfg is None:
                print(f"Не удалось подобрать конфиг источника тока для IDN: {src_idn}")
                return 1
        else:
            dmm_addr, dmm_cfg, src_addr, src_cfg = discover_instruments(
                MULTIMETER_CFG_DIR, SOURCE_CFG_DIR, rm=rm,
            )
    except RuntimeError as e:
        print(f"Ошибка обнаружения приборов: {e}")
        return 1

    try:
        relay_port = args.relay_port if args.relay_port else discover_relay_port()
    except RuntimeError as e:
        print(f"Ошибка обнаружения платы реле: {e}")
        return 1

    dmm = Multimeter(dmm_addr, dmm_cfg, rm=rm)
    src = CurrentSource(src_addr, src_cfg, rm=rm)
    relay = RelayController(relay_port)

    print("Приборы и реле инициализированы. Начинаю измерения...\n")

    try:
        results = run_measurement(
            dmm, src, relay,
            I_start=params['I_start'], I_stop=params['I_stop'], I_step=params['I_step'],
            V_limit=params['V_limit'], delay=params['delay'], cooling_delay=params['cooling_delay'],
        )
    finally:
        dmm.close()
        src.close()
        relay.close()

    print("\nИзмерения завершены, источник и реле выключены.")

    df = pd.DataFrame(results)
    with open(csv_path, 'w', encoding='utf-8') as f:
        f.write(f"# Датчик: {params['label']}\n")
        f.write(f"# Диапазон заданного тока: {params['I_start']}..{params['I_stop']} А, "
                f"шаг {params['I_step']} А, ограничение V={params['V_limit']} В\n")
        f.write(f"# Обе полярности сняты автоматически через плату реле (см. колонку Branch)\n")
        f.write(f"# Задержка установки: {params['delay']} с\n")
        f.write(f"# Задержка охлаждения: {params['cooling_delay']} с\n")
        f.write(f"# Время измерения: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# Всего точек: {len(df)}\n")
        f.write("#\n")
        df.to_csv(f, index=False)

    print(f"Данные сохранены в {csv_path}")
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

    stats = load_and_analyze(csv_path, I_nom=I_nom, X=X)

    print(f"\nГрафик сохранён: {stats['png_path']}")
    print(f"Максимальная приведённая погрешность: {stats['max_error_percent']:.4f} %")
    print(f"Средняя приведённая погрешность:   {stats['mean_error_percent']:.4f} %")

    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "measure":
        return cmd_measure(args)
    elif args.command == "analyze":
        return cmd_analyze(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
