"""Очистка данных о сообщениях в базе. Журнал аудита и его метки НЕ затрагиваются.

  python reset_data.py          — полная очистка сообщений, реакций, эмодзи (всё перечитается из Discord)
  python reset_data.py marks    — только сброс «водяных знаков»: история перечитается заново поверх
                                  существующих данных (обновятся реакции; удалённые сообщения останутся)
"""

import os
import sqlite3
import sys
from datetime import datetime

from config import DB_PATH
from db import get_db

# Таблицы, которые очищаются при полной очистке (всё, что можно перечитать из Discord).
# users НЕ очищается: там ники авторов действий из журнала аудита.
WIPE = ["messages", "mentions", "emoji", "emoji_info", "reactions",
        "reaction_users", "channels", "sync_runs"]

def backup() -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"{DB_PATH}.{stamp}.bak"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(path)
    src.backup(dst)                      # корректная копия, даже если есть файлы журнала WAL
    dst.close()
    src.close()
    return path

def main():
    if not os.path.exists(DB_PATH):
        print("Базы нет, чистить нечего.")
        return
    marks_only = len(sys.argv) > 1 and sys.argv[1].lower() == "marks"

    print(f"Резервная копия: {backup()}")
    db = get_db()
    audit_before = db.execute("SELECT COUNT(*) FROM audit_entries").fetchone()[0]

    if marks_only:
        with db:
            db.execute("UPDATE channels SET last_synced_id = 0")
        print("Водяные знаки сброшены. Следующий сбор перечитает всю историю поверх текущих данных.")
    else:
        with db:
            for table in WIPE:
                db.execute(f"DELETE FROM {table}")
        db.execute("VACUUM")             # возвращает освобождённое место на диске
        print("Данные о сообщениях очищены. Следующий сбор прочитает историю заново.")

    audit_after = db.execute("SELECT COUNT(*) FROM audit_entries").fetchone()[0]
    print(f"Журнал аудита: было {audit_before}, стало {audit_after} записей (должно совпадать).")

if __name__ == "__main__":
    main()