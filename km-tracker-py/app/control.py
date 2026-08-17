# ============================================================
# Дерево иерархии, поиск по коду, статистика — вкладка «Контроль».
# Один склад -> убрал фильтр по складу (был в мультискладской версии).
# Вместо него у каждого набора/товара виден station_id — какая из 20
# станций его произвела; это даёт супервайзеру ту же наблюдаемость,
# просто в разрезе станций, а не складов.
# ============================================================
from .db import conn, read_lock


def _pallet_node(p):
    boxes = conn.execute("SELECT * FROM boxes WHERE pallet_id=? ORDER BY closed_at", (p["id"],)).fetchall()
    return {
        'type': 'pallet', 'id': p["id"], 'code': p["pallet_code"], 'station_id': p["station_id"],
        'closed_at': p["closed_at"], 'boxes_count': p["boxes_count"],
        'children': [_box_node(b) for b in boxes],
    }


def _box_node(b):
    kits = conn.execute("SELECT * FROM kits WHERE box_id=? ORDER BY closed_at", (b["id"],)).fetchall()
    return {
        'type': 'box', 'id': b["id"], 'code': b["km_box_code"], 'station_id': b["station_id"],
        'closed_at': b["closed_at"], 'kits_count': b["kits_count"],
        'children': [_kit_node(k) for k in kits],
    }


def _kit_node(k):
    items = conn.execute("SELECT * FROM items WHERE kit_id=? ORDER BY id", (k["id"],)).fetchall()
    return {
        'type': 'kit', 'id': k["id"], 'code': k["km_agg_code"], 'name': k["kit_name"], 'station_id': k["station_id"],
        'status': k["status"], 'items_count': k["items_count"], 'items_required': k["items_required"],
        'opened_at': k["opened_at"], 'closed_at': k["closed_at"],
        'children': [
            {'type': 'item', 'id': i["id"], 'code': i["km_code"], 'name': i["item_name"],
             'station_id': i["station_id"], 'scanned_at': i["scanned_at"], 'children': []}
            for i in items
        ],
    }


def _node_matches(node, needle):
    hay = f'{node.get("code") or ""} {node.get("name") or ""} {node.get("station_id") or ""}'.lower()
    if needle in hay:
        return True
    return any(_node_matches(c, needle) for c in node.get('children', []))


def build_tree(q=None):
    with read_lock():
        pallets = conn.execute("SELECT * FROM pallets ORDER BY closed_at DESC").fetchall()
        boxes_no_pallet = conn.execute(
            "SELECT * FROM boxes WHERE pallet_id IS NULL AND status='closed' ORDER BY closed_at DESC"
        ).fetchall()
        kits_no_box = conn.execute(
            "SELECT * FROM kits WHERE box_id IS NULL AND status='closed' ORDER BY closed_at DESC"
        ).fetchall()
        open_kits = conn.execute(
            "SELECT * FROM kits WHERE status='open' ORDER BY opened_at DESC"
        ).fetchall()

        tree = (
            [_pallet_node(p) for p in pallets]
            + [_box_node(b) for b in boxes_no_pallet]
            + [_kit_node(k) for k in kits_no_box]
            + [_kit_node(k) for k in open_kits]
        )

    if q and q.strip():
        needle = q.strip().lower()
        tree = [n for n in tree if _node_matches(n, needle)]
    return tree


def search_code(raw_code):
    code = (raw_code or '').strip()
    if not code:
        return {'found': False}

    with read_lock():
        item = conn.execute("SELECT * FROM items WHERE km_code=?", (code,)).fetchone()
        if item:
            kit = conn.execute("SELECT * FROM kits WHERE id=?", (item["kit_id"],)).fetchone()
            box = conn.execute("SELECT * FROM boxes WHERE id=?", (kit["box_id"],)).fetchone() if kit and kit["box_id"] else None
            pallet = conn.execute("SELECT * FROM pallets WHERE id=?", (box["pallet_id"],)).fetchone() if box and box["pallet_id"] else None
            return {'found': True, 'type': 'item', 'item': dict(item), 'kit': dict(kit) if kit else None,
                    'box': dict(box) if box else None, 'pallet': dict(pallet) if pallet else None}

        kit = conn.execute("SELECT * FROM kits WHERE km_agg_code=?", (code,)).fetchone()
        if kit:
            box = conn.execute("SELECT * FROM boxes WHERE id=?", (kit["box_id"],)).fetchone() if kit["box_id"] else None
            pallet = conn.execute("SELECT * FROM pallets WHERE id=?", (box["pallet_id"],)).fetchone() if box and box["pallet_id"] else None
            items = conn.execute("SELECT * FROM items WHERE kit_id=?", (kit["id"],)).fetchall()
            return {'found': True, 'type': 'kit', 'kit': dict(kit), 'box': dict(box) if box else None,
                    'pallet': dict(pallet) if pallet else None, 'items': [dict(i) for i in items]}

        box = conn.execute("SELECT * FROM boxes WHERE km_box_code=?", (code,)).fetchone()
        if box:
            pallet = conn.execute("SELECT * FROM pallets WHERE id=?", (box["pallet_id"],)).fetchone() if box["pallet_id"] else None
            kits = conn.execute("SELECT * FROM kits WHERE box_id=?", (box["id"],)).fetchall()
            return {'found': True, 'type': 'box', 'box': dict(box), 'pallet': dict(pallet) if pallet else None,
                    'kits': [dict(k) for k in kits]}

        pallet = conn.execute("SELECT * FROM pallets WHERE pallet_code=?", (code,)).fetchone()
        if pallet:
            boxes = conn.execute("SELECT * FROM boxes WHERE pallet_id=?", (pallet["id"],)).fetchall()
            return {'found': True, 'type': 'pallet', 'pallet': dict(pallet), 'boxes': [dict(b) for b in boxes]}

    return {'found': False}


def get_stats():
    with read_lock():
        kits_closed = conn.execute("SELECT COUNT(*) c FROM kits WHERE status='closed'").fetchone()["c"]
        kits_open = conn.execute("SELECT COUNT(*) c FROM kits WHERE status='open'").fetchone()["c"]
        boxes_closed = conn.execute("SELECT COUNT(*) c FROM boxes").fetchone()["c"]
        pallets_closed = conn.execute("SELECT COUNT(*) c FROM pallets").fetchone()["c"]
        items_scanned = conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        kits_closed_today = conn.execute(
            "SELECT COUNT(*) c FROM kits WHERE status='closed' AND date(closed_at)=date('now','localtime')"
        ).fetchone()["c"]
        errors_today = conn.execute(
            "SELECT COUNT(*) c FROM scan_log WHERE result='error' AND date(created_at)=date('now','localtime')"
        ).fetchone()["c"]
        active_stations_today = conn.execute(
            "SELECT COUNT(DISTINCT station_id) c FROM scan_log WHERE date(created_at)=date('now','localtime')"
        ).fetchone()["c"]
        by_day = conn.execute(
            "SELECT date(closed_at) AS day, COUNT(*) AS kits FROM kits WHERE status='closed' "
            "GROUP BY date(closed_at) ORDER BY day DESC LIMIT 14"
        ).fetchall()

    return {
        'kitsClosed': kits_closed, 'kitsOpen': kits_open, 'boxesClosed': boxes_closed,
        'palletsClosed': pallets_closed, 'itemsScanned': items_scanned,
        'kitsClosedToday': kits_closed_today, 'errorsToday': errors_today,
        'activeStationsToday': active_stations_today,
        'byDay': [dict(r) for r in by_day],
    }
