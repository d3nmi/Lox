# ============================================================
# Бизнес-логика сканирования, до 20 одновременных станций одного склада:
#
#   - Оператор СНАЧАЛА ВЫБИРАЕТ набор на станции (select_kit), затем
#     сканирует его состав. Выбор хранится на сервере (station_selection)
#     и сохраняется между сборками: после закрытия набора выбранным
#     остаётся тот же набор — удобно собирать серию одинаковых.
#   - "Открытый набор" — состояние КОНКРЕТНОЙ СТАНЦИИ (station_id).
#   - Короб/паллета — общий пул склада: закрытие короба забирает ВСЕ
#     закрытые-но-неупакованные наборы от ЛЮБЫХ станций.
#
# Режим "Код короба" (галочка на станции, station_settings.require_box):
#   - включён: после агрегационного кода набор закрыт, но станция не
#     начнёт следующий набор, пока не будет отсканирован внутренний код
#     короба "DTV…" (он забирает закрытые наборы пула в короб);
#   - выключен: набор закрывается автоматически сразу после агрегационного
#     кода, код короба сканировать не нужно.
#
# НОМЕР НАБОРА (kits.kit_no) — сквозной порядковый номер набора по порядку
# закрытия (1, 2, 3…) среди всех станций. Присваивается в момент закрытия
# набора агрегационным кодом (внутри той же транзакции, поэтому номера не
# повторяются и не пропускаются) и не зависит от скана кода короба.
#
# Два вида позиций в составе набора:
#   marked=1 — товар с КМ (DataMatrix GS1 "01<GTIN14>21…"): уникален
#              глобально (used_codes), попадает в выгрузку;
#   marked=0 — упаковка/товар без КМ: сканируется обычным штрихкодом
#              (EAN-13/14) или артикулом, повторять можно (в used_codes
#              не пишется), в выгрузку не входит.
#
# КРИТИЧНО ДЛЯ КОНКУРЕНТНОСТИ: process_scan() держит ОДНУ транзакцию
# (одно удержание блокировки) на ВЕСЬ скан целиком — проверка дубля,
# определение типа кода и запись результата не разрываются на отдельные
# захваты блокировки.
# ============================================================
import re
import sqlite3
from .db import conn, transaction, read_lock, next_scan_order, lock

GTIN_RE = re.compile(r'^01(\d{14})')          # GS1-код (КМ товара / агрегата набора)
PLAIN_RE = re.compile(r'^\d{13,14}$')         # обычный штрихкод EAN-13 / GTIN-14
BOX_CODE_RE = re.compile(r'^DTV\d{10}$')      # реальный формат короба, пример: DTV0003111664
MAX_KITS_PER_BOX = 80  # временный лимит наборов на один короб — заменить на реальный

# Подставной GTIN для тестовой кнопки агрегата набора, у которого GTIN в справочнике не задан
FAKE_AGG_GTIN = '09999999999990'


def short_name(name):
    return (name or '').split(':')[0]


def extract_gtin14(code):
    m = GTIN_RE.match(code or '')
    return m.group(1) if m else None


def _resolve_gtin(code):
    """-> (gtin14 | None, is_gs1). Обычный EAN-13 приводится к GTIN-14 нулём слева."""
    m = GTIN_RE.match(code or '')
    if m:
        return m.group(1), True
    if PLAIN_RE.match(code or ''):
        return code.zfill(14), False
    return None, False


# ---- миграция: настройки станции и номер набора ----
def ensure_box_schema():
    """Вызывается один раз при старте (после init_db). Идемпотентна.
    Создаёт station_settings, добавляет kits.kit_no и проставляет его уже
    закрытым наборам (по порядку закрытия)."""
    with lock:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS station_settings ("
            "station_id TEXT PRIMARY KEY, "
            "require_box INTEGER NOT NULL DEFAULT 0)"
        )
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(kits)").fetchall()]
        if "kit_no" not in cols:
            conn.execute("ALTER TABLE kits ADD COLUMN kit_no INTEGER")
        conn.execute(
            "UPDATE kits SET kit_no = ("
            "  SELECT COUNT(*) FROM kits k2 WHERE k2.status='closed' AND k2.scan_order < kits.scan_order"
            ") + 1 "
            "WHERE status='closed' AND kit_no IS NULL"
        )


def _require_box(station_id):
    row = conn.execute(
        "SELECT require_box FROM station_settings WHERE station_id=?", (station_id,)
    ).fetchone()
    return bool(row and row["require_box"])


def _own_unboxed_count(station_id):
    return conn.execute(
        "SELECT COUNT(*) c FROM kits WHERE station_id=? AND status='closed' AND box_id IS NULL", (station_id,)
    ).fetchone()["c"]


def set_station_settings(station_id, require_box):
    with transaction():
        conn.execute(
            "INSERT INTO station_settings (station_id, require_box) VALUES (?, ?) "
            "ON CONFLICT(station_id) DO UPDATE SET require_box=excluded.require_box",
            (station_id, 1 if require_box else 0),
        )
    return {
        'result': 'ok',
        'message': ('Режим «Код короба» включён: после набора сканируйте код короба DTV…' if require_box
                    else 'Режим «Код короба» выключен: набор закрывается автоматически'),
    }


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


# ---- справочник наборов ----
def _template(kit_code):
    return conn.execute("SELECT * FROM kit_templates WHERE kit_code=?", (kit_code,)).fetchone()


def _template_for_open_kit(open_kit):
    """Шаблон набора, который сейчас собирается. Для наборов, начатых версией
    без kit_code, — запасной поиск по GTIN набора."""
    if open_kit["kit_code"]:
        return _template(open_kit["kit_code"])
    if open_kit["kit_sku"]:
        return conn.execute("SELECT * FROM kit_templates WHERE kit_sku=?", (open_kit["kit_sku"],)).fetchone()
    return None


def _template_items(template_id):
    return conn.execute(
        "SELECT * FROM kit_template_items WHERE kit_template_id=? ORDER BY id", (template_id,)
    ).fetchall()


def _missing_gtin_names(tpl_items):
    return [i["item_name"] for i in tpl_items if not i["item_sku"]]


def _match_slot(tpl_items, code):
    """Какую позицию состава представляет отсканированный код.
    -> (slot | None, problem | None); problem == 'need_km', если товар требует КМ,
    а отсканирован обычный штрихкод."""
    up = code.upper()
    for s in tpl_items:
        if not s["marked"] and s["item_code"].upper() == up:   # упаковку можно сканировать по артикулу
            return s, None
    gtin, is_gs1 = _resolve_gtin(code)
    if gtin:
        for s in tpl_items:
            if s["item_sku"] and s["item_sku"] == gtin:
                if s["marked"] and not is_gs1:
                    return None, 'need_km'
                return s, None
    return None, None


# ---- кандидаты наборов: автоопределение по отсканированным позициям ----
PENDING_NAME = 'Набор определяется…'   # служебное имя набора, у которого тип ещё не определён


def _is_pending(open_kit):
    return open_kit["kit_code"] is None and open_kit["kit_name"] == PENDING_NAME


def _ready_templates():
    """[(шаблон, позиции)] только наборов, у которых заполнены ВСЕ GTIN
    (иначе сервер не сможет их распознать по скану)."""
    out = []
    for tpl in conn.execute("SELECT * FROM kit_templates ORDER BY id").fetchall():
        slots = _template_items(tpl["id"])
        if not _missing_gtin_names(slots):
            out.append((tpl, slots))
    return out


def _kit_counts(kit_id):
    rows = conn.execute("SELECT item_sku, COUNT(*) c FROM items WHERE kit_id=? GROUP BY item_sku", (kit_id,)).fetchall()
    return {r["item_sku"]: r["c"] for r in rows}


def _fits(slots, counts):
    """Подходит ли набор под уже отсканированное: каждая позиция есть в составе и не больше нормы."""
    qty = {s["item_sku"]: s["qty_required"] for s in slots}
    return all(sku in qty and n <= qty[sku] for sku, n in counts.items())


def _candidates(counts):
    return [(t, s) for t, s in _ready_templates() if _fits(s, counts)]


def _lock_kit(kit_id, tpl, tpl_items):
    """Набор определён: записываем тип в открытый набор станции."""
    conn.execute(
        "UPDATE kits SET kit_code=?, kit_name=?, kit_sku=?, items_required=? WHERE id=?",
        (tpl["kit_code"], tpl["kit_name"], tpl["kit_sku"] or '', sum(i["qty_required"] for i in tpl_items), kit_id),
    )


def _names(cands):
    return '; '.join(f'«{t["kit_name"]}»' for t, _ in cands)


# ---- ручной выбор набора на станции (закрепление) или возврат к автоопределению ----
def select_kit(station_id, kit_code):
    kit_code = (kit_code or '').strip()
    with transaction():
        open_kit = conn.execute(
            "SELECT * FROM kits WHERE station_id=? AND status='open'", (station_id,)
        ).fetchone()

        if kit_code in ('', 'auto'):
            conn.execute("DELETE FROM station_selection WHERE station_id=?", (station_id,))
            return {'result': 'ok', 'message': 'Набор определяется автоматически по сканированию КМ'}

        tpl = _template(kit_code)
        if not tpl:
            return {'result': 'error', 'message': 'Такого набора нет в справочнике'}
        tpl_items = _template_items(tpl["id"])
        missing = _missing_gtin_names(tpl_items)
        if missing:
            return {'result': 'error',
                    'message': f'Набор «{tpl["kit_name"]}» пока нельзя собирать — в справочнике не заполнены GTIN '
                               f'({len(missing)} поз.)'}
        if open_kit:
            if _is_pending(open_kit):
                if not _fits(tpl_items, _kit_counts(open_kit["id"])):
                    return {'result': 'error',
                            'message': f'Уже отсканированные позиции не входят в набор «{tpl["kit_name"]}»'}
                _lock_kit(open_kit["id"], tpl, tpl_items)
            else:
                open_tpl = _template_for_open_kit(open_kit)
                if not open_tpl or open_tpl["kit_code"] != tpl["kit_code"]:
                    return {'result': 'error',
                            'message': f'Сначала завершите набор «{short_name(open_kit["kit_name"])}» на этой станции'}
        conn.execute(
            "INSERT INTO station_selection (station_id, kit_code) VALUES (?, ?) "
            "ON CONFLICT(station_id) DO UPDATE SET kit_code=excluded.kit_code, "
            "updated_at=datetime('now','localtime')",
            (station_id, tpl["kit_code"]),
        )
        return {'result': 'ok', 'message': f'Выбран набор «{tpl["kit_name"]}»'}


# ---- работа с коробом (общий пул всех станций); вызывается УЖЕ внутри транзакции ----
#
# Короб не закрывается одним сканом. Реальный код короба (DTV…) — это
# постоянная наклейка на физическом коробе, и её сканируют ПОВТОРНО каждый
# раз, когда в короб кладут очередную порцию готовых наборов — пока короб
# не наполнится до MAX_KITS_PER_BOX. Поэтому пока короб не заполнен, его
# код НЕ попадает в used_codes — в used_codes код короба попадает только
# в момент фактического закрытия.
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
        "SELECT * FROM kits WHERE status='closed' AND box_id IS NULL ORDER BY closed_at, id"
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


# ---- запись одной позиции состава; вызывается УЖЕ внутри транзакции ----
def _insert_item(station_id, code, is_gs1, slot, kit_id):
    marked = 1 if slot["marked"] else 0
    # Упаковка без КМ сканируется одним и тем же штрихкодом многократно, а km_code в БД
    # UNIQUE — поэтому для не-GS1 кода добавляем служебный суффикс порядка скана.
    km_code = code if is_gs1 else f'{code}#{next_scan_order()}'
    conn.execute(
        "INSERT INTO items (km_code, item_sku, item_name, kit_id, station_id, scanned_at, marked) "
        "VALUES (?, ?, ?, ?, ?, datetime('now','localtime'), ?)",
        (km_code, slot["item_sku"], slot["item_name"], kit_id, station_id, marked),
    )
    if is_gs1:
        _mark_used(code, 'item', station_id)


def _unrecognized(code):
    return (f'Код "{code[:24]}" не распознан (ожидается КМ товара, штрихкод/артикул упаковки, '
            f'код короба "DTV…" или паллеты "PLT-…")')


NEED_KM = 'Для этого товара нужен код маркировки (DataMatrix), а не штрихкод — отсканируйте КМ с упаковки'


# ---- закрытие набора агрегационным кодом; набор УЖЕ определён ----
def _close_kit(station_id, code, gtin, is_gs1, kit_id, tpl, slots):
    name = short_name(tpl["kit_name"])
    if not is_gs1:
        return _fail(station_id, code, 'kit_agg', f'Состав набора «{name}» полон — ожидается его агрегационный код (GS1)')
    expected = tpl["kit_sku"]
    if expected:
        if gtin != expected:
            return _fail(
                station_id, code, 'kit_agg',
                f'Состав набора «{name}» полон — ожидается его агрегационный код, отсканирован иной GTIN {gtin}'
            )
    else:
        # GTIN набора в справочнике не задан — принимаем любой GS1-код, который не товар из состава
        if any(i["item_sku"] == gtin for i in slots):
            return _fail(
                station_id, code, 'kit_agg',
                f'Состав набора «{name}» полон — ожидается агрегационный код набора, а это код товара из состава'
            )

    # Номер набора присваивается СРАЗУ при закрытии (до UPDATE — считаем, сколько
    # наборов уже закрыто). Всё внутри одной транзакции, поэтому номера не повторяются.
    closed_before = conn.execute("SELECT COUNT(*) c FROM kits WHERE status='closed'").fetchone()["c"]
    kit_no = closed_before + 1

    conn.execute(
        "UPDATE kits SET status='closed', km_agg_code=?, kit_no=?, closed_at=datetime('now','localtime'), scan_order=? WHERE id=?",
        (code, kit_no, next_scan_order(), kit_id),
    )
    _mark_used(code, 'kit_agg', station_id)

    # Отдельная выгрузочная таблица: по строке на каждый товар С КМ закрытого набора
    # (упаковка без КМ в выгрузку не входит); код короба DTV… пока неизвестен (NULL).
    kit_items = conn.execute("SELECT km_code FROM items WHERE kit_id=? AND marked=1", (kit_id,)).fetchall()
    conn.executemany(
        "INSERT INTO export_data (kit_id, km_agg_code, km_code) VALUES (?, ?, ?)",
        [(kit_id, code, it["km_code"]) for it in kit_items],
    )

    in_pool = conn.execute("SELECT COUNT(*) c FROM kits WHERE status='closed' AND box_id IS NULL").fetchone()["c"]
    if _require_box(station_id):
        return _ok(station_id, code, 'kit_agg',
                   f'Набор №{kit_no} закрыт · теперь отсканируйте код короба (DTV…) · наборов в пуле: {in_pool}')
    return _ok(station_id, code, 'kit_agg',
               f'Набор №{kit_no} закрыт автоматически · наборов в общем пуле: {in_pool}')


# ---- набор определён, состав ещё не полон ----
def _add_to_locked(station_id, code, is_gs1, gtin, open_kit, tpl, slots):
    slot, problem = _match_slot(slots, code)
    if slot is None:
        if problem == 'need_km':
            return _fail(station_id, code, 'item', NEED_KM)
        if gtin:
            return _fail(station_id, code, 'item', f'Товар с GTIN {gtin} не входит в состав набора «{short_name(open_kit["kit_name"])}»')
        return _fail(station_id, code, 'item', _unrecognized(code))

    scanned_for_this = conn.execute(
        "SELECT COUNT(*) c FROM items WHERE kit_id=? AND item_sku=?", (open_kit["id"], slot["item_sku"])
    ).fetchone()["c"]
    if scanned_for_this >= slot["qty_required"]:
        return _fail(station_id, code, 'item', f'Норма по «{slot["item_name"]}» уже выполнена ({slot["qty_required"]} шт) для текущего набора')

    _insert_item(station_id, code, is_gs1, slot, open_kit["id"])
    conn.execute("UPDATE kits SET items_count = items_count + 1 WHERE id=?", (open_kit["id"],))
    total = open_kit["items_count"] + 1
    return _ok(station_id, code, 'item', f'Принято: «{slot["item_name"]}» ({total}/{open_kit["items_required"]})')


# ---- набор ещё НЕ определён (или станция только начинает): определяем по сканам ----
def _add_to_undetermined(station_id, code, is_gs1, gtin, open_kit, cands, counts, pinned):
    hits = []
    problem = None
    norm_slot = None
    for tpl, slots in cands:
        slot, prob = _match_slot(slots, code)
        if slot is None:
            problem = problem or prob
            continue
        if counts.get(slot["item_sku"], 0) >= slot["qty_required"]:
            norm_slot = slot
            continue
        hits.append((tpl, slots, slot))

    if not hits:
        # возможно, это агрегационный код набора, у которого состав уже полон (набор различим только по нему)
        if open_kit and is_gs1:
            complete = [(t, s) for t, s in cands if all(counts.get(x["item_sku"], 0) == x["qty_required"] for x in s)]
            match = [(t, s) for t, s in complete if t["kit_sku"] == gtin]
            if not match:
                nulls = [(t, s) for t, s in complete if not t["kit_sku"]]
                if len(nulls) == 1 and not any(x["item_sku"] == gtin for x in nulls[0][1]):
                    match = nulls
            if len(match) == 1:
                tpl, slots = match[0]
                _lock_kit(open_kit["id"], tpl, slots)
                return _close_kit(station_id, code, gtin, True, open_kit["id"], tpl, slots)
        if norm_slot:
            return _fail(station_id, code, 'item', f'Норма по «{norm_slot["item_name"]}» уже выполнена ({norm_slot["qty_required"]} шт) для текущего набора')
        if problem == 'need_km':
            return _fail(station_id, code, 'item', NEED_KM)
        if not gtin:
            return _fail(station_id, code, 'unknown', _unrecognized(code))
        if pinned:
            return _fail(station_id, code, 'unknown', f'Товар с GTIN {gtin} не входит в состав выбранного набора «{pinned["kit_name"]}»')
        if open_kit:
            return _fail(station_id, code, 'item',
                         f'Товар с GTIN {gtin} не подходит к начатому набору. Возможные наборы: {_names(cands)}')
        return _fail(station_id, code, 'unknown',
                     f'GTIN {gtin} не найден ни в одном наборе справочника (или у набора не заполнены GTIN)')

    slot = hits[0][2]
    new_cands = [(t, s) for t, s, _ in hits]

    if open_kit:
        kit_id = open_kit["id"]
        prev_count = open_kit["items_count"]
    else:
        cur = conn.execute(
            "INSERT INTO kits (kit_sku, kit_name, kit_code, station_id, status, items_count, items_required, scan_order, opened_at) "
            "VALUES ('', ?, NULL, ?, 'open', 0, 0, ?, datetime('now','localtime'))",
            (PENDING_NAME, station_id, next_scan_order()),
        )
        kit_id = cur.lastrowid
        prev_count = 0

    _insert_item(station_id, code, is_gs1, slot, kit_id)
    conn.execute("UPDATE kits SET items_count = items_count + 1 WHERE id=?", (kit_id,))
    total = prev_count + 1

    if len(new_cands) == 1:
        tpl, slots = new_cands[0]
        _lock_kit(kit_id, tpl, slots)
        required = sum(i["qty_required"] for i in slots)
        if prev_count == 0:
            return _ok(station_id, code, 'item', f'Начат набор «{short_name(tpl["kit_name"])}» · позиция 1/{required} принята')
        return _ok(station_id, code, 'item', f'Набор определён: «{short_name(tpl["kit_name"])}» · принято {total}/{required}')
    return _ok(station_id, code, 'item',
               f'Принято: «{slot["item_name"]}» · набор определится по следующей позиции (варианты: {_names(new_cands)})')


# ---- товар либо агрегационный код набора; вызывается УЖЕ внутри транзакции ----
def _handle_item_or_kit_agg(station_id, code):
    gtin, is_gs1 = _resolve_gtin(code)

    open_kit = conn.execute(
        "SELECT * FROM kits WHERE station_id=? AND status='open'", (station_id,)
    ).fetchone()

    # --- включён режим «Код короба»: пока не отсканирован короб для закрытого набора, новый не начинаем ---
    if not open_kit and _require_box(station_id) and _own_unboxed_count(station_id) > 0:
        return _fail(station_id, code, 'unknown',
                     'Набор закрыт — сначала отсканируйте код короба (DTV…), затем начинайте следующий набор')

    # --- набор уже определён ---
    if open_kit and not _is_pending(open_kit):
        tpl = _template_for_open_kit(open_kit)
        if not tpl:
            return _fail(station_id, code, 'unknown',
                         f'Набор «{short_name(open_kit["kit_name"])}» не найден в справочнике — обратитесь к администратору')
        slots = _template_items(tpl["id"])
        if open_kit["items_count"] < open_kit["items_required"]:
            return _add_to_locked(station_id, code, is_gs1, gtin, open_kit, tpl, slots)
        return _close_kit(station_id, code, gtin, is_gs1, open_kit["id"], tpl, slots)

    # --- набор ещё не определён: ищем среди подходящих ---
    pinned = None
    if open_kit:   # начат, но по уже принятым позициям вариантов несколько
        counts = _kit_counts(open_kit["id"])
        cands = _candidates(counts)
        if not cands:
            return _fail(station_id, code, 'unknown', 'Принятые позиции не подходят ни к одному набору справочника — обратитесь к администратору')
    else:
        counts = {}
        sel = conn.execute("SELECT kit_code FROM station_selection WHERE station_id=?", (station_id,)).fetchone()
        if sel:   # набор закреплён вручную
            tpl = _template(sel["kit_code"])
            if not tpl:
                return _fail(station_id, code, 'unknown', 'Выбранный набор не найден в справочнике — выберите набор заново')
            slots = _template_items(tpl["id"])
            if _missing_gtin_names(slots):
                return _fail(station_id, code, 'unknown', f'Набор «{tpl["kit_name"]}» пока нельзя собирать — в справочнике не заполнены GTIN')
            cands = [(tpl, slots)]
            pinned = tpl
        else:
            cands = _ready_templates()
            if not cands:
                return _fail(station_id, code, 'unknown', 'В справочнике нет ни одного набора с заполненными GTIN')
    return _add_to_undetermined(station_id, code, is_gs1, gtin, open_kit, cands, counts, pinned)


def process_scan(station_id, raw_code):
    """Один скан = одна непрерывная транзакция от первой до последней строки."""
    code = (raw_code or '').strip()

    with transaction():
        if not code:
            return _fail(station_id, '', None, 'Пустой скан — код не распознан')

        # Глобальная проверка дубля — только для кодов с уникальным серийником:
        # GS1-коды (КМ, агрегат), короба и паллеты. Обычный штрихкод/артикул упаковки
        # одинаков у всех экземпляров и дублем не считается.
        if GTIN_RE.match(code) or BOX_CODE_RE.match(code) or code.startswith('PLT-'):
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
            # Второй рубеж защиты от дублей/гонок — UNIQUE-констрейнты в схеме.
            return _fail(station_id, code, 'unknown', f'Код уже был отсканирован ранее (дубль): {code}')


# ---- снимок текущего состояния консоли станции ----
def get_station_state(station_id):
    with read_lock():
        open_kit = conn.execute(
            "SELECT * FROM kits WHERE station_id=? AND status='open'", (station_id,)
        ).fetchone()
        sel = conn.execute("SELECT kit_code FROM station_selection WHERE station_id=?", (station_id,)).fetchone()
        pinned_tpl = _template(sel["kit_code"]) if sel else None
        mode = 'pinned' if pinned_tpl else 'auto'

        kit = None
        tpl = None
        pending = None
        counts = {}
        if open_kit:
            items = conn.execute("SELECT * FROM items WHERE kit_id=? ORDER BY id", (open_kit["id"],)).fetchall()
            for i in items:
                counts[i["item_sku"]] = counts.get(i["item_sku"], 0) + 1
            is_pending = _is_pending(open_kit)
            kit = {
                'kit_name': None if is_pending else open_kit["kit_name"],
                'kit_sku': open_kit["kit_sku"],
                'kit_code': open_kit["kit_code"],
                'pending': is_pending,
                'items_count': open_kit["items_count"],
                'items_required': open_kit["items_required"],
                'items': [dict(i) for i in items],
            }
            if is_pending:
                pending = {
                    'candidates': [{'kit_code': t["kit_code"], 'kit_name': t["kit_name"]} for t, _ in _candidates(counts)],
                    'scanned': [{'item_name': i["item_name"], 'marked': bool(i["marked"])} for i in items],
                }
            else:
                tpl = _template_for_open_kit(open_kit)
        elif pinned_tpl:
            tpl = pinned_tpl

        selected = None
        checklist = []
        if tpl:
            for s in _template_items(tpl["id"]):
                checklist.append({
                    'item_code': s["item_code"], 'item_name': s["item_name"], 'item_sku': s["item_sku"],
                    'marked': bool(s["marked"]), 'required': s["qty_required"],
                    'scanned': min(counts.get(s["item_sku"], 0) if s["item_sku"] else 0, s["qty_required"]),
                })
            selected = {
                'kit_code': tpl["kit_code"], 'kit_name': tpl["kit_name"], 'kit_sku': tpl["kit_sku"],
                'locked': bool(open_kit),   # набор уже начат — сменить нельзя
                'agg_ready': bool(open_kit) and open_kit["items_count"] >= open_kit["items_required"],
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

        require_box = _require_box(station_id)

        return {
            'mode': mode,
            'currentKit': kit,
            'selectedKit': selected,
            'pending': pending,
            'checklist': checklist,
            'requireBox': require_box,
            'awaitingBox': bool(require_box and not open_kit and _own_unboxed_count(station_id) > 0),
            'kitsInBox': kits_in_pool,
            'boxesOnPallet': boxes_in_pool,
            'kitsAssembled': kits_assembled,
            'feed': [dict(f) for f in feed],
        }
