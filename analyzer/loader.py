"""Единая точка входа для чтения данных."""

from dataset import Dataset, LoaderError, SyncInfo
from loader_db import load_db
from loader_xlsx import discover, load_dataset

__all__ = ["Dataset", "LoaderError", "SyncInfo", "discover", "load_dataset", "load_db"]