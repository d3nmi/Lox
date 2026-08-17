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
BOX_CODE_RE = re.compile(r'^DTV\d{10}$')  # реальный формат короба, пример: DTV0003111664
MAX_KITS_PER_BOX = 80  # временный лимит наборов на один короб — заменить на реальный


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


# ---- работа с коробом (общий пул всех станций); вызывается УЖЕ внутри транзакции ----
#
# Короб не закрывается одним сканом. Реальный код короба (DTV…) — это
# постоянная наклейка на физическом коробе, и её сканируют ПОВТОРНО каждый
# раз, когда в короб кладут очередную порцию готовых наборов — пока короб
# не наполнится до MAX_KITS_PER_BOX. Поэтому пока короб не заполнен, его
# код НЕ попадает в used_codes (иначе повторный скан тут же отбивался бы
# глобальной проверкой дубля в process_scan, до входа в эту функцию) —
# в used_codes код короба попадает только в момент фактического закрытия.
def _handle_box_code(station_id, code):
    own_open_kit = conn.execute(
        "SELECT * FROM kits WHERE station_id=? AND status='open'", (station_id,)
    ).fetchone()
    if own_open_kit:
        return _fail(
            station_id, code, 'box_agg',
            f'Нельзя работать с коробом — на вашей станции набор «{short_name(own_open_kit["kit_name"])}» ещё не завершён'
        )

    orphan_kits_all = conn.execute(
        "SELECT * FROM kits WHERE status='closed' AND box_id IS NULL ORDER BY closed_at"
    ).fetchall()

    existing_box = conn.execute(
        "SELECT * FROM boxes WHERE km_box_code=? AND status='open'", (code,)
    ).fetchone()

    if existing_box:
        capacity_left = MAX_KITS_PER_BOX - existing_box["kits_count"]
        take = orphan_kits_all[:capacity_left]
        if not take:
            return _fail(station_id, code, 'box_agg', 'В общем пуле нет новых наборов для добавления в этот короб')
        box_id = existing_box["id"]
        new_count = existing_box["kits_count"] + len(take)
    else:
        if not orphan_kits_all:
            return _fail(station_id, code, 'box_agg', 'Нельзя открыть короб — в общем пуле нет ни одного набора')
        take = orphan_kits_all[:MAX_KITS_PER_BOX]
        new_count = len(take)

    now_closing = new_count >= MAX_KITS_PER_BOX

    if existing_box:
        if now_closing:
            conn.execute(
                "UPDATE boxes SET kits_count=?, status='closed', closed_at=datetime('now','localtime'), scan_order=? WHERE id=?",
                (new_count, next_scan_order(), box_id),
            )
        else:
            conn.execute(
                "UPDATE boxes SET kits_count=?, scan_order=? WHERE id=?",
                (new_count, next_scan_order(), box_id),
            )
    else:
        status = 'closed' if now_closing else 'open'
        closed_at_sql = "datetime('now','localtime')" if now_closing else "NULL"
        cur = conn.execute(
            "INSERT INTO boxes (km_box_code, station_id, status, kits_count, scan_order, opened_at, closed_at) "
            f"VALUES (?, ?, ?, ?, ?, datetime('now','localtime'), {closed_at_sql})",
            (code, station_id, status, new_count, next_scan_order()),
        )
        box_id = cur.lastrowid

    conn.executemany("UPDATE kits SET box_id=? WHERE id=?", [(box_id, k["id"]) for k in take])
    # Код короба на момент закрытия наборов ещё не был известен (см. ниже,
    # где заполняется export_data) — дополняем его теперь. Строки в
    # export_data уже существуют для каждого вложения этих наборов, здесь
    # только простановка box-кода (актуален даже пока короб ещё открыт).
    conn.executemany(
        "UPDATE export_data SET km_box_code=? WHERE kit_id=?",
        [(code, k["id"]) for k in take],
    )

    remaining_pool = len(orphan_kits_all) - len(take)

    if now_closing:
        _mark_used(code, 'box_agg', station_id)
        extra = f', ещё {remaining_pool} наборов осталось в пуле на следующий короб' if remaining_pool > 0 else ''
        return _ok(station_id, code, 'box_agg', f'Короб закрыт: {new_count}/{MAX_KITS_PER_BOX} наборов внутри, из разных станций{extra}')
    else:
        extra = f', ещё {remaining_pool} наборов осталось в пуле' if remaining_pool > 0 else ''
        return _ok(
            station_id, code, 'box_agg',
            f'В короб добавлено {len(take)} набор(ов), итого {new_count}/{MAX_KITS_PER_BOX}{extra} — '
            f'короб пока открыт, отсканируйте тот же код ещё раз, чтобы добавить наборы'
        )


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
            f'код короба "DTV…" / паллеты "PLT-…")'
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

    # Отдельная выгрузочная таблица (не зависит от items/kits/boxes):
    # по строке на каждое вложение закрытого набора, код короба пока
    # неизвестен (наполняемость короба наборами станет ясна позже, при
    # закрытии короба — см. _handle_box_code) и до этого момента остаётся NULL.
    kit_items = conn.execute("SELECT km_code FROM items WHERE kit_id=?", (open_kit["id"],)).fetchall()
    conn.executemany(
        "INSERT INTO export_data (kit_id, km_agg_code, km_code) VALUES (?, ?, ?)",
        [(open_kit["id"], code, it["km_code"]) for it in kit_items],
    )

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
            if BOX_CODE_RE.match(code):
                return _handle_box_code(station_id, code)
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
