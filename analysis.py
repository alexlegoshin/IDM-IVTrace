import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

try:
    from scipy.interpolate import CubicSpline
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


def find_latest_csv(data_dir: Path) -> Path:
    """Возвращает самый свежий IVtrace_*.csv файл в data_dir."""
    data_dir = Path(data_dir)
    if not data_dir.exists():
        raise FileNotFoundError(f"Папка {data_dir} не найдена. Сначала выполните измерения.")

    csv_files = list(data_dir.glob("IVtrace_*.csv"))
    if not csv_files:
        raise FileNotFoundError(f"Нет CSV-файлов IVtrace_*.csv в папке {data_dir}.")

    return max(csv_files, key=lambda f: f.stat().st_mtime)


def _read_metadata(csv_path: Path) -> dict:
    """Читает строки, начинающиеся с '#', в виде 'ключ: значение'."""
    metadata = {}
    with open(csv_path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.startswith('#'):
                break
            match = re.match(r'#\s*(.*?)\s*:\s*(.*)', line)
            if match:
                key, value = match.groups()
                metadata[key.strip()] = value.strip()
    return metadata


def load_and_analyze(latest_csv: Path, I_nom: float, X: float, save_png: bool = True) -> dict:
    """
    Читает CSV с результатами измерения, считает приведённую погрешность
    относительно ожидаемого выходного тока датчика (коэффициент 1:X),
    строит график (амплитудная характеристика + погрешность) и сохраняет PNG
    рядом с CSV.

    Возвращает словарь со статистикой и путями к файлам.
    """
    latest_csv = Path(latest_csv)
    metadata = _read_metadata(latest_csv)
    df = pd.read_csv(latest_csv, comment='#')

    if df.empty:
        raise ValueError(f"Файл {latest_csv} не содержит данных измерений.")

    label = metadata.get('Датчик', 'Неизвестный датчик')
    direction = metadata.get('ветвь', '?')
    I_start = df['I_set_A'].min()
    I_stop = df['I_set_A'].max()

    # ---------- Расчёт погрешности ----------
    K = 1.0 / X                      # коэффициент передачи I_out / I_in
    I_sec_nom = I_nom * K            # номинальный выходной ток при I_nom

    df['I_expected_A'] = df['I_set_A'] * K
    df['Error_percent'] = np.abs(df['I_meas_A'] - df['I_expected_A']) / I_sec_nom * 100

    # ---------- Построение графиков ----------
    plt.style.use('default')
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 10), sharex=True,
        gridspec_kw={'height_ratios': [3, 1]},
    )
    fig.patch.set_facecolor('white')
    ax1.set_facecolor('white')
    ax2.set_facecolor('white')
    ax1.minorticks_on()
    ax2.minorticks_on()

    x_label = f"1:{int(X)}" if float(X).is_integer() else f"1:{X:.1f}"

    # Верхний график: выходной ток
    ax1.plot(df['I_set_A'], df['I_meas_A'], 'o-', color='steelblue', markersize=4,
              label=f'{label} ({direction}) – измер.')
    ax1.plot(df['I_set_A'], df['I_expected_A'], '--', color='orange', linewidth=1.5,
              label=f'Ожидаемый ({x_label})')
    ax1.set_ylabel('Выходной ток датчика, А')
    ax1.set_title(f'Амплитудная характеристика датчика тока\nДиапазон {I_start}..{I_stop} А')
    ax1.legend(loc='upper left')
    ax1.grid(True, which='major', linestyle='-', linewidth=0.6, alpha=0.7)
    ax1.grid(True, which='minor', linestyle=':', linewidth=0.4, alpha=0.5)

    # Нижний график: приведённая погрешность
    x = df['I_set_A'].values
    y = df['Error_percent'].values
    order = np.argsort(x)
    x_sorted, y_sorted = x[order], y[order]

    if SCIPY_AVAILABLE and len(x_sorted) > 3:
        cs = CubicSpline(x_sorted, y_sorted)
        x_smooth = np.linspace(x_sorted[0], x_sorted[-1], 500)
        ax2.plot(x_smooth, cs(x_smooth), '-', color='firebrick', linewidth=1.2,
                  label='Погрешность приведённая')
    else:
        ax2.plot(x_sorted, y_sorted, '-', color='firebrick', linewidth=1.2,
                  label='Погрешность приведённая')

    ax2.plot(df['I_set_A'], df['Error_percent'], 'x', color='firebrick', markersize=6, alpha=0.7)
    ax2.axhline(y=0, color='gray', linewidth=0.5)
    ax2.set_xlabel('Заданный первичный ток $I_{уст}$, А')
    ax2.set_ylabel('Погрешность, %')
    ax2.legend(loc='upper right')
    ax2.grid(True, which='major', linestyle='-', linewidth=0.6, alpha=0.7)
    ax2.grid(True, which='minor', linestyle=':', linewidth=0.4, alpha=0.5)

    plt.tight_layout()

    png_path: Optional[Path] = None
    if save_png:
        png_path = latest_csv.with_suffix('.png')
        plt.savefig(png_path, dpi=150, bbox_inches='tight')

    plt.close(fig)

    stats = {
        'csv_path': latest_csv,
        'png_path': png_path,
        'label': label,
        'direction': direction,
        'I_start': I_start,
        'I_stop': I_stop,
        'I_nom': I_nom,
        'X': X,
        'points': len(df),
        'max_error_percent': float(df['Error_percent'].max()),
        'mean_error_percent': float(df['Error_percent'].mean()),
        'dataframe': df,
    }
    return stats
