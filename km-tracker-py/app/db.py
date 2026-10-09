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
#
# СПРАВОЧНИК НАБОРОВ — файл kits.csv РЯДОМ с программой (рядом с exe / в корне
# проекта при обычном запуске). Редактируется без пересборки, читается при
# каждом старте сервера. Если файла нет — создаётся из встроенного
# app/config/kit-templates.json и дальше источником правды служит CSV.
# ============================================================
import csv
import os
import re
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
    (например, снимок состояния станции из нескольких отдельных запросов),
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


def _drop_templates():
    """Справочник наборов — производная от kits.csv: пересоздаётся при каждом
    старте (формат таблиц изменился — старые определения подлежат замене).
    На эти таблицы нет внешних ключей из рабочих данных, отсканированное
    не затрагивается. Сперва дочерняя таблица, затем родительская."""
    with _lock:
        conn.execute("DROP TABLE IF EXISTS kit_template_items")
        conn.execute("DROP TABLE IF EXISTS kit_templates")


def _init_schema():
    schema_sql = (resource_dir() / "app" / "schema.sql").read_text(encoding="utf-8")
    with _lock:
        conn.executescript(schema_sql)


def _ensure_column(table, column, ddl):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate():
    """Догоняет БД, созданные более ранней версией программы (CREATE TABLE IF NOT
    EXISTS не меняет уже существующие таблицы). На свежей БД — ничего не делает."""
    with _lock:
        _ensure_column("kits", "kit_code", "TEXT")
        _ensure_column("items", "marked", "INTEGER NOT NULL DEFAULT 1")


# ============================================================
# Справочник наборов: kits.csv
#
# Одна строка = одна позиция состава набора. Столбцы (разделитель ";"):
#   1. Название набора
#   2. GTIN набора            (агрегационный код; можно оставить пустым)
#   3. Название товара
#   4. GTIN товара            (13 или 14 цифр; пустой — набор пока нельзя собирать)
#   5. Количество             (пусто = 1)
#   6. С маркировкой (КМ)     да / нет  (пусто = да; «нет» — упаковка со штрихкодом)
#
# Название и GTIN набора можно писать в каждой строке состава или только в
# первой — пустые ячейки берутся от набора выше. Строки с "#" в начале —
# комментарии.
# ============================================================
KITS_CSV_NAME = "kits.csv"
KITS_CSV_HEADER = [
    "Название набора", "GTIN набора", "Название товара",
    "GTIN товара", "Количество", "С маркировкой (КМ)",
]
_YES = {"", "да", "д", "yes", "y", "true", "1", "км"}
_NO = {"нет", "н", "no", "n", "false", "0", "шк"}


def kits_csv_path():
    return app_dir() / KITS_CSV_NAME


def _write_kits_csv(path, templates):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(KITS_CSV_HEADER)
        for t in templates:
            for it in t["items"]:
                w.writerow([
                    t["kit_name"], t.get("kit_sku") or "",
                    it["item_name"], it.get("item_sku") or "",
                    it["qty_required"], "да" if it.get("marked", True) else "нет",
                ])


def _bootstrap_kits_csv(path):
    """Первый запуск: создаём kits.csv из встроенного JSON (или пустой шаблон)."""
    templates = []
    try:
        src = resource_dir() / "app" / "config" / "kit-templates.json"
        data = json.loads(src.read_text(encoding="utf-8"))
        if isinstance(data, list):
            templates = [t for t in data if isinstance(t, dict) and isinstance(t.get("items"), list)]
    except (OSError, json.JSONDecodeError):
        templates = []
    try:
        _write_kits_csv(path, templates)
    except OSError as err:
        print(f"\n[КМ-сервер] Не удалось создать справочник {path}:\n  {err}\n")
        sys.exit(1)
    print(f"[КМ-сервер] Создан справочник наборов: {path}")
    print("            Правьте его (Блокнот / Excel), затем перезапустите сервер.")


def _norm_gtin(raw, where, errors):
    s = (raw or "").strip().replace(" ", "")
    if s.startswith("'"):
        s = s[1:]
    if not s:
        return None
    if re.search(r"[eE][+\-]?\d", s) or "," in s or "." in s:
        errors.append(
            f'{where}: «{raw}» — похоже, Excel превратил GTIN в число (вид 8,05E+12). '
            f'Сделайте столбец текстовым и впишите GTIN заново.'
        )
        return None
    if not s.isdigit():
        errors.append(f'{where}: GTIN «{raw}» должен состоять только из цифр.')
        return None
    if len(s) < 12 or len(s) > 14:
        errors.append(f'{where}: GTIN «{raw}» — {len(s)} цифр, ожидается 13 или 14.')
        return None
    return s.zfill(14)   # 13-значный EAN и потерянный Excel-ом ведущий ноль → GTIN-14


def _read_kits_csv(path):
    """-> (templates | None, errors). Формат шаблонов — тот же, что у kit-templates.json."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp1251")   # CSV из русской версии Excel
    except OSError as err:
        return None, [f"Не удалось прочитать файл: {err}"]

    first = next((ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")), "")
    delim = ";" if first.count(";") >= first.count(",") else ","

    errors = []
    kits = {}          # название набора -> шаблон (порядок появления сохраняется)
    current = None
    for n, row in enumerate(csv.reader(text.splitlines(), delimiter=delim), start=1):
        cells = [c.strip() for c in row] + [""] * 6
        if not any(cells[:6]) or cells[0].startswith("#"):
            continue
        if n == 1 and cells[0].lower().startswith("название набора"):
            continue
        where = f"Строка {n}"
        kit_name, kit_gtin_raw, item_name, item_gtin_raw, qty_raw, marked_raw = cells[:6]

        if kit_name:
            if kit_name not in kits:
                kits[kit_name] = {"kit_code": f"К-{len(kits) + 1}", "kit_name": kit_name,
                                  "kit_sku": None, "items": []}
            current = kits[kit_name]
        elif current is None:
            errors.append(f"{where}: не указано название набора.")
            continue

        kit_gtin = _norm_gtin(kit_gtin_raw, f"{where}, GTIN набора", errors)
        if kit_gtin:
            if current["kit_sku"] and current["kit_sku"] != kit_gtin:
                errors.append(f"{where}: у набора «{current['kit_name']}» указаны разные GTIN набора.")
            current["kit_sku"] = kit_gtin

        if not item_name and not item_gtin_raw:
            continue   # строка только с названием/GTIN набора
        if not item_name:
            errors.append(f"{where}: не указано название товара.")
            continue

        item_gtin = _norm_gtin(item_gtin_raw, f"{where}, GTIN товара", errors)

        if qty_raw == "":
            qty = 1
        else:
            try:
                qty = int(qty_raw)
            except ValueError:
                qty = 0
            if qty < 1:
                errors.append(f"{where}: «Количество» должно быть целым числом от 1, сейчас «{qty_raw}».")
                continue

        m = marked_raw.lower()
        if m in _YES:
            marked = True
        elif m in _NO:
            marked = False
        else:
            errors.append(f"{where}: «С маркировкой» — впишите «да» или «нет», сейчас «{marked_raw}».")
            continue

        current["items"].append({
            "item_code": item_gtin or item_name,
            "item_sku": item_gtin,
            "item_name": item_name,
            "qty_required": qty,
            "marked": marked,
        })

    templates = list(kits.values())
    for t in templates:
        if not t["items"]:
            errors.append(f"Набор «{t['kit_name']}»: не указано ни одной позиции состава.")
    return (None if errors else templates), errors


def _load_and_validate_templates():
    """Читает и проверяет справочник kits.csv ДО того, как к БД прикоснулись
    хоть одним запросом — чтобы при ошибке в справочнике файл km.db действительно
    не менялся (иначе сообщение об ошибке было бы неправдой)."""
    path = kits_csv_path()
    if not path.exists():
        _bootstrap_kits_csv(path)

    templates, errors = _read_kits_csv(path)
    if not errors:
        if not templates:
            errors = ["Справочник пуст — впишите хотя бы один набор и его состав."]
        else:
            _, errors = validate_kit_templates(templates)

    if errors:
        print(f"\n[КМ-сервер] Справочник наборов {path} содержит ошибки:")
        for i, e in enumerate(errors, 1):
            print(f"  {i}. {e}")
        print("\nСервер не запущен — исправьте файл и запустите снова. Рабочие данные в БД не затронуты (файл km.db мог появиться пустым, 0 байт, — его можно спокойно удалить).\n")
        sys.exit(1)

    return templates


def _seed_templates(templates):
    with transaction():
        for t in templates:
            cur = conn.execute(
                "INSERT INTO kit_templates (kit_code, kit_sku, kit_name) VALUES (?, ?, ?)",
                (t["kit_code"], t.get("kit_sku"), t["kit_name"]),
            )
            kit_template_id = cur.lastrowid
            for it in t["items"]:
                conn.execute(
                    "INSERT INTO kit_template_items "
                    "(kit_template_id, item_code, item_sku, item_name, qty_required, marked) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (kit_template_id, it["item_code"], it.get("item_sku"), it["item_name"],
                     it["qty_required"], 1 if it.get("marked", True) else 0),
                )


def init_db():
    templates = _load_and_validate_templates()  # ничего не пишет и не читает из БД
    _drop_templates()
    _init_schema()
    _migrate()
    _seed_templates(templates)
