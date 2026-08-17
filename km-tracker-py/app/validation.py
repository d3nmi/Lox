# ============================================================
# Бизнес-логика сканирования. Соответствует Этапу 2 плана, адаптировано
# под 20 одновременных станций одного склада:
#
#   - "Открытый набор" — состояние КОНКРЕТНОЙ СТАНЦИИ (station_id).
#     20 станций одновременно ведут 20 независимых наборов.
#   - Короб/паллета — общий пул склада: закрытие короба забирает ВСЕ
#     закрытые-но-неупакованные наборы от ЛЮБЫХ станций, а не только
#     от той, что сканирует агрегат короба.
#
# КРИТИЧНО ДЛЯ КОНКУРЕНТНОСТИ: process_scan() держит ОДНУ транзакцию
# (одно удержание блокировки) на ВЕСЬ скан целиком — проверка дубля,
# определение типа кода и запись результата не разрываются на отдельные
# захваты блокировки. FastAPI выполняет синхронные роуты в пуле потоков
# ОС (это не однопоточный Node.js), поэтому если бы проверка дубля и
# запись были в разных захватах блокировки, два потока (две станции),
# отсканировавшие один и тот же код почти одновременно, теоретически
# могли бы оба пройти проверку "не дубль" до того, как кто-то из них
# успеет записать used_codes. Один непрерывный BEGIN IMMEDIATE...COMMIT
# на весь скан исключает это полностью.
# ============================================================
import re
import sqlite3
from .db import conn, transaction, read_lock, next_scan_order

GTIN_RE = re.compile(r'^01(\d{14})')


def short_name(name):
    return (name or '').split(':')[0]


def extract_gtin14(code):
    m = GTIN_RE.match(code or '')
    return m.group(1) if m else None


def _mark_used(code, code_type, station_id):
    conn.execute(
        "INSERT INTO used_codes (code, code_type, station_id) VALUES (?, ?, ?)",
        (code, code_type, station_id),
    )


def _write_log(station_id, code, code_type, result, message):
    conn.execute(
        "INSERT INTO scan_log (station_id, code, code_type, result, message) VALUES (?, ?, ?, ?, ?)",
        (station_id, code, code_type, result, message),
    )


def _fail(station_id, code, code_type, message):
    _write_log(station_id, code, code_type, 'error', message)
    return {'result': 'error', 'message': message, 'code_type': code_type or 'unknown'}


def _ok(station_id, code, code_type, message):
    _write_log(station_id, code, code_type, 'ok', message)
    return {'result': 'ok', 'message': message, 'code_type': code_type}


# ---- закрытие короба (общий пул всех станций); вызывается УЖЕ внутри транзакции ----
def _close_box(station_id, code):
    own_open_kit = conn.execute(
        "SELECT * FROM kits WHERE station_id=? AND status='open'", (station_id,)
    ).fetchone()
    if own_open_kit:
        return _fail(
            station_id, code, 'box_agg',
            f'Нельзя закрыть короб — на вашей станции набор «{short_name(own_open_kit["kit_name"])}» ещё не завершён'
        )

    orphan_kits = conn.execute(
        "SELECT * FROM kits WHERE status='closed' AND box_id IS NULL ORDER BY closed_at"
    ).fetchall()
    if not orphan_kits:
        return _fail(station_id, code, 'box_agg', 'Нельзя закрыть короб — в него не помещено ни одного набора')

    cur = conn.execute(
        "INSERT INTO boxes (km_box_code, station_id, status, kits_count, scan_order, opened_at, closed_at) "
        "VALUES (?, ?, 'closed', ?, ?, datetime('now','localtime'), datetime('now','localtime'))",
        (code, station_id, len(orphan_kits), next_scan_order()),
    )
    box_id = cur.lastrowid
    conn.executemany("UPDATE kits SET box_id=? WHERE id=?", [(box_id, k["id"]) for k in orphan_kits])
    _mark_used(code, 'box_agg', station_id)
    return _ok(station_id, code, 'box_agg', f'Короб закрыт ({len(orphan_kits)} наборов внутри, из разных станций)')


# ---- закрытие паллеты (общий пул всех станций); вызывается УЖЕ внутри транзакции ----
def _close_pallet(station_id, code):
    orphan_boxes = conn.execute(
        "SELECT * FROM boxes WHERE status='closed' AND pallet_id IS NULL ORDER BY closed_at"
    ).fetchall()
    if not orphan_boxes:
        return _fail(station_id, code, 'pallet', 'Нельзя закрыть паллету — на неё не выложено ни одного короба')

    cur = conn.execute(
        "INSERT INTO pallets (pallet_code, station_id, status, boxes_count, scan_order, opened_at, closed_at) "
        "VALUES (?, ?, 'closed', ?, ?, datetime('now','localtime'), datetime('now','localtime'))",
        (code, station_id, len(orphan_boxes), next_scan_order()),
    )
    pallet_id = cur.lastrowid
    conn.executemany("UPDATE boxes SET pallet_id=? WHERE id=?", [(pallet_id, b["id"]) for b in orphan_boxes])
    _mark_used(code, 'pallet', station_id)
    return _ok(station_id, code, 'pallet', f'Паллета закрыта ({len(orphan_boxes)} коробов)')


# ---- товар либо агрегационный код набора; вызывается УЖЕ внутри транзакции ----
def _handle_item_or_kit_agg(station_id, code):
    gtin = extract_gtin14(code)
    if not gtin:
        return _fail(
            station_id, code, 'unknown',
            f'Код "{code[:24]}" не распознан (ожидается GS1-код товара/набора или '
            f'агрегат короба "BOXAGG…" / паллеты "PLT-…")'
        )

    open_kit = conn.execute(
        "SELECT * FROM kits WHERE station_id=? AND status='open'", (station_id,)
    ).fetchone()

    # --- нет открытого набора на этой станции: ждём первый товар ---
    if not open_kit:
        tpl = conn.execute(
            "SELECT kt.* FROM kit_templates kt "
            "JOIN kit_template_items kti ON kti.kit_template_id = kt.id "
            "WHERE kti.item_sku = ? LIMIT 1",
            (gtin,),
        ).fetchone()
        if not tpl:
            return _fail(station_id, code, 'unknown', f'GTIN {gtin} не найден ни в одном шаблоне набора — требуется уточнение справочника')

        tpl_items = conn.execute(
            "SELECT * FROM kit_template_items WHERE kit_template_id=?", (tpl["id"],)
        ).fetchall()
        items_required = sum(i["qty_required"] for i in tpl_items)
        need = next(i for i in tpl_items if i["item_sku"] == gtin)

        cur = conn.execute(
            "INSERT INTO kits (kit_sku, kit_name, station_id, status, items_count, items_required, scan_order, opened_at) "
            "VALUES (?, ?, ?, 'open', 1, ?, ?, datetime('now','localtime'))",
            (tpl["kit_sku"], tpl["kit_name"], station_id, items_required, next_scan_order()),
        )
        kit_id = cur.lastrowid
        conn.execute(
            "INSERT INTO items (km_code, item_sku, item_name, kit_id, station_id, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))",
            (code, gtin, need["item_name"], kit_id, station_id),
        )
        _mark_used(code, 'item', station_id)
        return _ok(station_id, code, 'item', f'Начат набор «{short_name(tpl["kit_name"])}» · товар 1/{items_required} принят')

    # --- набор открыт, состав ещё не полон: ждём ещё товар ---
    if open_kit["items_count"] < open_kit["items_required"]:
        tpl_items = conn.execute(
            "SELECT kti.* FROM kit_template_items kti "
            "JOIN kit_templates kt ON kt.id = kti.kit_template_id "
            "WHERE kt.kit_sku = ?",
            (open_kit["kit_sku"],),
        ).fetchall()
        need = next((i for i in tpl_items if i["item_sku"] == gtin), None)
        if need is None:
            return _fail(station_id, code, 'item', f'Товар с GTIN {gtin} не входит в состав набора «{short_name(open_kit["kit_name"])}»')

        scanned_for_this = conn.execute(
            "SELECT COUNT(*) c FROM items WHERE kit_id=? AND item_sku=?", (open_kit["id"], gtin)
        ).fetchone()["c"]
        if scanned_for_this >= need["qty_required"]:
            return _fail(station_id, code, 'item', f'Норма по «{need["item_name"]}» уже выполнена ({need["qty_required"]} шт) для текущего набора')

        conn.execute(
            "INSERT INTO items (km_code, item_sku, item_name, kit_id, station_id, scanned_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now','localtime'))",
            (code, gtin, need["item_name"], open_kit["id"], station_id),
        )
        conn.execute("UPDATE kits SET items_count = items_count + 1 WHERE id=?", (open_kit["id"],))
        _mark_used(code, 'item', station_id)
        total = open_kit["items_count"] + 1
        return _ok(station_id, code, 'item', f'Товар принят: «{need["item_name"]}» ({total}/{open_kit["items_required"]})')

    # --- состав полон: ждём именно агрегационный код ЭТОГО набора ---
    if gtin != open_kit["kit_sku"]:
        return _fail(
            station_id, code, 'kit_agg',
            f'Состав набора «{short_name(open_kit["kit_name"])}» полон — ожидается его агрегационный код, отсканирован иной GTIN {gtin}'
        )

    conn.execute(
        "UPDATE kits SET status='closed', km_agg_code=?, closed_at=datetime('now','localtime'), scan_order=? WHERE id=?",
        (code, next_scan_order(), open_kit["id"]),
    )
    _mark_used(code, 'kit_agg', station_id)
    in_pool = conn.execute("SELECT COUNT(*) c FROM kits WHERE status='closed' AND box_id IS NULL").fetchone()["c"]
    return _ok(station_id, code, 'kit_agg', f'Набор закрыт агрегационным кодом · наборов в общем пуле на короб: {in_pool}')


def process_scan(station_id, raw_code):
    """Один скан = одна непрерывная транзакция от первой до последней строки:
    проверка дубля, маршрутизация по типу кода и запись результата — без
    разрывов между захватами блокировки (см. комментарий в шапке файла)."""
    code = (raw_code or '').strip()

    with transaction():
        if not code:
            return _fail(station_id, '', None, 'Пустой скан — код не распознан')

        dup = conn.execute("SELECT code_type FROM used_codes WHERE code=?", (code,)).fetchone()
        if dup:
            return _fail(station_id, code, dup["code_type"], f'Код уже был отсканирован ранее (дубль): {code}')

        try:
            if code.startswith('BOXAGG'):
                return _close_box(station_id, code)
            if code.startswith('PLT-'):
                return _close_pallet(station_id, code)
            return _handle_item_or_kit_agg(station_id, code)
        except sqlite3.IntegrityError:
            # Второй рубеж защиты от дублей/гонок — если два потока каким-то
            # образом одновременно прошли проверку "не дубль" (при нашей модели
            # с единой блокировкой это не должно случаться, но UNIQUE-констрейнты
            # в схеме стоят намеренно как независимая гарантия на уровне БД).
            return _fail(station_id, code, 'unknown', f'Код уже был отсканирован ранее (дубль): {code}')


# ---- снимок текущего состояния консоли станции ----
def get_station_state(station_id):
    with read_lock():
        open_kit = conn.execute(
            "SELECT * FROM kits WHERE station_id=? AND status='open'", (station_id,)
        ).fetchone()
        kit = None
        if open_kit:
            items = conn.execute("SELECT * FROM items WHERE kit_id=? ORDER BY id", (open_kit["id"],)).fetchall()
            kit = {
                'kit_name': open_kit["kit_name"],
                'kit_sku': open_kit["kit_sku"],
                'items_count': open_kit["items_count"],
                'items_required': open_kit["items_required"],
                'items': [dict(i) for i in items],
            }

        kits_in_pool = conn.execute("SELECT COUNT(*) c FROM kits WHERE status='closed' AND box_id IS NULL").fetchone()["c"]
        boxes_in_pool = conn.execute("SELECT COUNT(*) c FROM boxes WHERE status='closed' AND pallet_id IS NULL").fetchone()["c"]
        kits_assembled = conn.execute(
            "SELECT COUNT(*) c FROM kits WHERE station_id=? AND status='closed'", (station_id,)
        ).fetchone()["c"]
        feed = conn.execute(
            "SELECT created_at AS t, result, message FROM scan_log WHERE station_id=? ORDER BY id DESC LIMIT 60",
            (station_id,),
        ).fetchall()

        return {
            'currentKit': kit,
            'kitsInBox': kits_in_pool,
            'boxesOnPallet': boxes_in_pool,
            'kitsAssembled': kits_assembled,
            'feed': [dict(f) for f in feed],
        }
