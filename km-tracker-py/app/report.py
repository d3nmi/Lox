# ============================================================
# Выгрузка данных. Формат:
#   Код идентификации набора ; Номер набора ;
#   Наименование товара ; Код маркировки
# В выгрузку входят только товары с КМ из ЗАКРЫТЫХ наборов (упаковка без
# КМ не входит). Номер набора (kits.kit_no) — сквозной порядковый номер
# набора по порядку закрытия (1, 2, 3…), присваивается сразу при закрытии
# набора агрегационным кодом (см. validation._close_kit). Технические
# поля (станция, время, короб, паллета) — только в БД (вкладка «Контроль»).
# ============================================================
from .db import conn, read_lock

_REPORT_SQL = """
    SELECT k.km_agg_code AS kit_agg_code, k.kit_no AS kit_no,
           i.item_name AS item_name, i.km_code AS km_code
    FROM items i
    JOIN kits k ON k.id = i.kit_id AND k.status = 'closed'
    WHERE i.marked = 1
    ORDER BY k.id DESC, i.id DESC
    {limit_sql}
"""


def report_rows(limit=None):
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    with read_lock():
        rows = conn.execute(_REPORT_SQL.format(limit_sql=limit_sql)).fetchall()
    return [dict(r) for r in rows]


def report_count():
    with read_lock():
        return conn.execute(
            "SELECT COUNT(*) c FROM items i JOIN kits k ON k.id = i.kit_id AND k.status='closed' WHERE i.marked = 1"
        ).fetchone()["c"]


def _csv_escape(val):
    if val is None:
        return ''
    s = str(val)
    if any(c in s for c in (';', '"', '\n', '\r')):
        return '"' + s.replace('"', '""') + '"'
    return s


def export_csv():
    rows = list(reversed(report_rows(None)))  # хронологический порядок в файле
    header = ['Код идентификации набора', 'Номер набора', 'Наименование товара', 'Код маркировки']
    lines = [';'.join(_csv_escape(h) for h in header)]
    for r in rows:
        lines.append(';'.join(_csv_escape(v) for v in (r['kit_agg_code'], r['kit_no'], r['item_name'], r['km_code'])))
    return '﻿' + '\r\n'.join(lines)  # BOM для корректной кириллицы в Excel


# ============================================================
# Отдельная выгрузка: набор / вложение / короб.
# Источник — таблица export_data, а не items/kits/boxes напрямую:
# строка на каждое вложение появляется в момент закрытия набора, код
# короба в ней сперва NULL и заполняется позже, когда набор попадает в короб.
# ============================================================
def export_data_rows(limit=None):
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    with read_lock():
        rows = conn.execute(f"""
            SELECT km_agg_code AS kit_agg_code, km_code AS item_km_code, km_box_code AS box_km_code
            FROM export_data
            ORDER BY id DESC
            {limit_sql}
        """).fetchall()
    return [dict(r) for r in rows]


def export_data_count():
    with read_lock():
        return conn.execute("SELECT COUNT(*) c FROM export_data").fetchone()["c"]


def export_data_csv():
    rows = list(reversed(export_data_rows(None)))  # хронологический порядок в файле
    header = ['Код набора', 'Код вложения', 'Код короба']
    lines = [';'.join(_csv_escape(h) for h in header)]
    for r in rows:
        lines.append(';'.join(_csv_escape(v) for v in (r['kit_agg_code'], r['item_km_code'], r['box_km_code'])))
    return '﻿' + '\r\n'.join(lines)  # box_km_code будет пуст, пока набор не в коробе
