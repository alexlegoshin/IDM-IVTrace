#!/usr/bin/env python
"""
Единая точка входа IVTrace (используется и при сборке в exe).

  IVTrace.exe              -> графический интерфейс (GUI)
  IVTrace.exe gui          -> то же самое
  IVTrace.exe measure ...  -> измерение из командной строки
  IVTrace.exe analyze ...  -> анализ из командной строки

Вся логика диспетчеризации живёт в run.main; здесь только тонкая обёртка,
чтобы у exe было осмысленное имя точки входа.
"""
import sys

from run import main

if __name__ == "__main__":
    sys.exit(main())
