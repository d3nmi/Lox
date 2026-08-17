# ============================================================
# Автоматическое резервное копирование БД (Этап 3.4 плана).
# Используется встроенный в стандартную библиотеку Python
# sqlite3.Connection.backup() — стабильный API (не experimental,
# в отличие от node:sqlite backup() в Node-версии), ничего
# дополнительно устанавливать не нужно.
# ============================================================
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from .db import conn, DATA_DIR, lock

BACKUP_DIR = Path(DATA_DIR) / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
RETENTION = 60  # хранить последние N бэкапов


def _timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def run_backup(reason="scheduled"):
    dest_path = BACKUP_DIR / f"km_{_timestamp()}.db"
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        with lock:
            conn.backup(dest_conn)  # атомарный бэкап "на лету"
    finally:
        dest_conn.close()
    _cleanup_old_backups()
    return dest_path


def _cleanup_old_backups():
    files = sorted(BACKUP_DIR.glob("km_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[RETENTION:]:
        try:
            old.unlink()
        except OSError:
            pass


def list_backups():
    files = sorted(BACKUP_DIR.glob("km_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{'file': f.name, 'size': f.stat().st_size, 'mtime': f.stat().st_mtime} for f in files]


def schedule_backups(interval_minutes=15):
    def loop():
        while True:
            time.sleep(interval_minutes * 60)
            try:
                run_backup("scheduled")
            except Exception as err:  # фоновый поток не должен ронять процесс
                print(f"[backup] ошибка автосохранения: {err}")

    t = threading.Thread(target=loop, daemon=True)
    t.start()
