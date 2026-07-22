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


def load_and_analyze(latest_csv: Path, I_nom: float, X: float, save_png: bool = True,
                     show: bool = False, close_fig: bool = True) -> dict:
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
    # 'Branch' присутствует в новых файлах (forward/reverse через реле).
    # Для старых файлов без реле (одна ветвь, метаданные 'ветвь') подстраховываемся.
    has_branch_col = 'Branch' in df.columns
    if not has_branch_col:
        df['Branch'] = metadata.get('ветвь', 'forward')

    # Колонка возбуждения называется X_set (новые файлы, могут быть током
    # или напряжением — единица берётся из метаданных) либо I_set_A
    # (старые файлы до появления выбора типа возбуждения — всегда ток).
    if 'X_set' in df.columns:
        excitation_col = 'X_set'
        excitation_unit = metadata.get('Единица измерения возбуждения', 'А')
        excitation_type = metadata.get('Тип возбуждения', 'current')
    else:
        excitation_col = 'I_set_A'
        excitation_unit = 'А'
        excitation_type = 'current'

    excitation_label = 'ток' if excitation_type == 'current' else 'напряжение'

    X_start = df[excitation_col].min()
    X_stop = df[excitation_col].max()

    # ---------- Расчёт погрешности ----------
    # Погрешность всегда считается относительно выходного тока датчика
    # (I_meas_A), независимо от того, чем датчик возбуждался.
    K = 1.0 / X                      # коэффициент передачи I_out / X_in
    I_sec_nom = I_nom * K            # номинальный выходной ток при I_nom

    df['I_expected_A'] = df[excitation_col] * K
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

    # Верхний график: выходной ток. Forward и reverse рисуются отдельно —
    # это две разные ветви одного и того же прохода через 0, их нельзя
    # просто сортировать вместе по excitation_col без учёта знака.
    branch_styles = {
        'forward': dict(color='steelblue', marker='o', label=f'{label} (forward) – измер.'),
        'reverse': dict(color='seagreen', marker='s', label=f'{label} (reverse) – измер.'),
    }
    for branch_name, sub in df.groupby('Branch', sort=False):
        sub = sub.sort_values(excitation_col)
        style = branch_styles.get(branch_name, dict(color='gray', marker='.', label=f'{label} ({branch_name})'))
        ax1.plot(sub[excitation_col], sub['I_meas_A'], marker=style['marker'], linestyle='-',
                  color=style['color'], markersize=4, label=style['label'])

    df_sorted_for_expected = df.sort_values(excitation_col)
    ax1.plot(df_sorted_for_expected[excitation_col], df_sorted_for_expected['I_expected_A'], '--',
              color='orange', linewidth=1.5, label=f'Ожидаемый ({x_label})')
    ax1.set_ylabel('Выходной ток датчика, А')
    ax1.set_title(f'Амплитудная характеристика датчика тока (возбуждение — {excitation_label})\n'
                  f'Диапазон {X_start}..{X_stop} {excitation_unit}')
    ax1.legend(loc='upper left')
    ax1.grid(True, which='major', linestyle='-', linewidth=0.6, alpha=0.7)
    ax1.grid(True, which='minor', linestyle=':', linewidth=0.4, alpha=0.5)

    # Нижний график: приведённая погрешность.
    # ВАЖНО: forward и reverse обе проходят через excitation_col=0, так что
    # в объединённых данных x не строго возрастает (дубли на 0, а иногда и
    # на других точках при неровном шаге). CubicSpline требует строго
    # возрастающую последовательность x, поэтому дубли усредняем перед
    # построением сплайна — сами измеренные точки (крестики) остаются
    # нетронутыми и показывают обе ветви как есть.
    x = df[excitation_col].values
    y = df['Error_percent'].values
    order = np.argsort(x)
    x_sorted, y_sorted = x[order], y[order]

    x_unique, unique_idx, counts = np.unique(x_sorted, return_index=True, return_counts=True)
    y_avg = np.array([
        y_sorted[start:start + count].mean()
        for start, count in zip(unique_idx, counts)
    ])

    if SCIPY_AVAILABLE and len(x_unique) > 3:
        cs = CubicSpline(x_unique, y_avg)
        x_smooth = np.linspace(x_unique[0], x_unique[-1], 500)
        ax2.plot(x_smooth, cs(x_smooth), '-', color='firebrick', linewidth=1.2,
                  label='Погрешность приведённая (сглаженная)')
    else:
        ax2.plot(x_unique, y_avg, '-', color='firebrick', linewidth=1.2,
                  label='Погрешность приведённая')

    ax2.plot(df[excitation_col], df['Error_percent'], 'x', color='firebrick', markersize=6, alpha=0.7)
    ax2.axhline(y=0, color='gray', linewidth=0.5)
    ax2.set_xlabel(f'Заданное возбуждение ({excitation_label}), {excitation_unit}')
    ax2.set_ylabel('Погрешность, %')
    ax2.legend(loc='upper right')
    ax2.grid(True, which='major', linestyle='-', linewidth=0.6, alpha=0.7)
    ax2.grid(True, which='minor', linestyle=':', linewidth=0.4, alpha=0.5)

    plt.tight_layout()

    png_path: Optional[Path] = None
    if save_png:
        png_path = latest_csv.with_suffix('.png')
        plt.savefig(png_path, dpi=150, bbox_inches='tight')

    if show:
        plt.show()

    # close_fig=False используется GUI, чтобы встроить фигуру в окно
    # (FigureCanvasTkAgg); в этом случае ответственность за close — на вызове.
    if close_fig:
        plt.close(fig)

    branches_present = sorted(df['Branch'].unique().tolist())

    stats = {
        'csv_path': latest_csv,
        'png_path': png_path,
        'label': label,
        'branches': branches_present,
        'excitation_type': excitation_type,
        'excitation_unit': excitation_unit,
        'X_start': X_start,
        'X_stop': X_stop,
        'I_nom': I_nom,
        'X': X,
        'points': len(df),
        'max_error_percent': float(df['Error_percent'].max()),
        'mean_error_percent': float(df['Error_percent'].mean()),
        'dataframe': df,
        'figure': None if close_fig else fig,
    }
    return stats
