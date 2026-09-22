#!/usr/bin/env python3
"""Точка входа для панелей вроде Pterodactyl, где запускается один файл.

Эквивалент `python -m radar` / команды `radar`. Аргументы передаются как есть:
`python bot.py run`, `python bot.py init-db`, `python bot.py check-token`.
По умолчанию (без аргументов) запускается бот.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar.cli import main  # путь к пакету добавляется выше

if __name__ == "__main__":
    main()
