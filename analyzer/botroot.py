"""Подключает корень проекта бота статистики к sys.path, чтобы анализатор мог импортировать
config.py и rules.py бота. Корень добавляется В КОНЕЦ: одноимённые модули анализатора важнее."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))