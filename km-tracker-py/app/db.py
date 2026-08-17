# ============================================================
# Подключение к SQLite и модель конкурентности для до 20 станций.
#
# Модель нарочно простая и предсказуемая (по требованию — "максимально
# простая, стабильная"), а не максимально параллельная:
#
#   - ОДНО соединение sqlite3 на весь процесс (check_same_thread=False,
#     isolation_level=None — сами управляем транзакциями через BEGIN/COMMIT).
#   - ОДНА threading.RLock() сериализует АБСОЛЮТНО ВСЕ обращения к БД —
#     и чтения, и записи. Это осознанный выбор: при 20 станциях складского
#     темпа сканирования (не сотни операций в секунду) операции занимают
#     доли миллисекунды, и сериализация не создаёт заметной очереди, зато
#     полностью исключает гонки между потоками FastAPI/Uvicorn (каждый
#     синхронный роут в FastAPI выполняется в отдельном потоке пула).
#   - WAL + busy_timeout — вторая линия защиты на случай, если БД открыта
#     ВТОРЫМ процессом (например, ручное резервное копирование снаружи или
#     сторонний SQLite-браузер): вместо мгновенной ошибки "database is
#     locked" сервер подождёт до busy_timeout и повторит попытку сам.
#   - UNIQUE-констрейнты (used_codes, partial index на открытый набор) —
#     третья, независимая от кода приложения линия защиты от дублей и гонок
#     прямо на уровне движка БД.
# ============================================================
import os
import sys
import sqlite3
import threading
import json
from pathlib import Path

from .config_validate import validate_kit_templates
from .paths import resource_dir, app_dir

DATA_DIR = os.environ.get("KM_DATA_DIR")
DATA_DIR = Path(DATA_DIR) if DATA_DIR else (app_dir() / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "backups").mkdir(parents=True, exist_ok=True)

DB_PATH = os.environ.get("KM_DB_PATH")
DB_PATH = Path(DB_PATH) if DB_PATH else (DATA_DIR / "km.db")

_lock = threading.RLock()
lock = _lock  # публичный алиас для модулей, которым нужен прямой доступ к блокировке (backup.py)

conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, isolation_level=None)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode = WAL")
conn.execute("PRAGMA foreign_keys = ON")
conn.execute("PRAGMA busy_timeout = 5000")
conn.execute("PRAGMA synchronous = NORMAL")


def query_all(sql, params=()):
    with _lock:
        return conn.execute(sql, params).fetchall()


def query_one(sql, params=()):
    with _lock:
        cur = conn.execute(sql, params)
        return cur.fetchone()


def execute(sql, params=()):
    """Разовая запись ВНЕ явной транзакции (для одиночных UPDATE/INSERT
    без нескольких взаимосвязанных шагов). Коммитится немедленно."""
    with _lock:
        cur = conn.execute(sql, params)
        return cur


class transaction:
    """Контекстный менеджер многошаговой транзакции.
    Держит глобальную блокировку на всё время блока — гарантирует, что
    ни один другой поток (другая станция) не увидит и не создаст
    промежуточное/гонка-состояние между шагами. При исключении — ROLLBACK,
    БД остаётся в состоянии "как будто скана не было".
    """
    def __enter__(self):
        _lock.acquire()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
        finally:
            _lock.release()
        return False


class read_lock:
    """Лёгкий контекст: только блокировка, без BEGIN/COMMIT. Нужен, когда
    несколько SELECT подряд должны видеть один консистентный снимок
    (например, снимок состояния станции из пяти отдельных запросов),
    но сами по себе они не пишут в БД и полноценная транзакция избыточна.
    """
    def __enter__(self):
        _lock.acquire()
        return conn

    def __exit__(self, exc_type, exc, tb):
        _lock.release()
        return False


def next_scan_order():
    """ВАЖНО: всегда вызывается изнутри уже открытой транзакции (см. validation.py) —
    поэтому здесь НЕТ своего with transaction(): вложенный BEGIN IMMEDIATE внутри уже
    открытой транзакции SQLite не поддерживает и упадёт с OperationalError."""
    conn.execute("UPDATE scan_seq SET val = val + 1 WHERE id = 1")
    return conn.execute("SELECT val FROM scan_seq WHERE id = 1").fetchone()["val"]


def _init_schema():
    schema_sql = (resource_dir() / "app" / "schema.sql").read_text(encoding="utf-8")
    with _lock:
        conn.executescript(schema_sql)


def _load_and_validate_templates():
    """Читает и проверяет kit-templates.json ДО того, как к БД прикоснулись
    хоть одним запросом — чтобы при битом конфиге файл km.db действительно
    не создавался и не менялся (иначе сообщение об ошибке было бы неправдой)."""
    config_path = resource_dir() / "app" / "config" / "kit-templates.json"
    try:
        templates = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        print(f"\n[КМ-сервер] Не удалось прочитать app/config/kit-templates.json:\n  {err}")
        print("  Проверьте, что файл — валидный JSON (пропущенная запятая/скобка — самая частая причина).\n")
        sys.exit(1)

    valid, errors = validate_kit_templates(templates)
    if not valid:
        print("\n[КМ-сервер] Справочник наборов app/config/kit-templates.json содержит ошибки:")
        for i, e in enumerate(errors, 1):
            print(f"  {i}. {e}")
        print("\nСервер не запущен — исправьте файл и запустите снова. Ни одной таблицы/строки в БД не создано (файл km.db мог появиться пустым, 0 байт, — его можно спокойно удалить).\n")
        sys.exit(1)

    return templates


def _seed_templates(templates):
    count = query_one("SELECT COUNT(*) AS c FROM kit_templates")["c"]
    if count:
        return

    with transaction():
        for t in templates:
            cur = conn.execute(
                "INSERT INTO kit_templates (kit_sku, kit_name) VALUES (?, ?)",
                (t["kit_sku"], t["kit_name"]),
            )
            kit_template_id = cur.lastrowid
            for it in t["items"]:
                conn.execute(
                    "INSERT INTO kit_template_items (kit_template_id, item_sku, item_name, qty_required) "
                    "VALUES (?, ?, ?, ?)",
                    (kit_template_id, it["item_sku"], it["item_name"], it["qty_required"]),
                )


def init_db():
    templates = _load_and_validate_templates()  # ничего не пишет и не читает из БД
    _init_schema()
    _seed_templates(templates)
