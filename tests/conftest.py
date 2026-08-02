"""Скрипты лежат в scripts/ без пакета — добавляем каталог в путь импорта."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
