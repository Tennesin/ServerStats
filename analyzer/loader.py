"""Единая точка входа для чтения данных."""
from dataset import Dataset, LoaderError, SyncInfo
from loader_db import load_db

__all__ = ["Dataset", "LoaderError", "SyncInfo", "load_db"]