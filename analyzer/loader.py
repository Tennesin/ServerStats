"""Единая точка входа для чтения данных.
Сейчас источник один — xlsx; чтение из БД (loader_db.py) подключается на следующем этапе.
Остальные модули импортируют отсюда: from loader import Dataset, LoaderError, ..."""

from dataset import Dataset, LoaderError, SyncInfo
from loader_xlsx import discover, load_dataset

__all__ = ["Dataset", "LoaderError", "SyncInfo", "discover", "load_dataset"]