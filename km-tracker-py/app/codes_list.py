# ============================================================
# Список отсканированных кодов и поиск (страница «Контроль», вкладка «Список кодов»).
# Источник — журнал сканов scan_log (там все сканы, и принятые, и с ошибкой),
# к каждой строке подтягивается набор, короб и номер набора, куда код попал.
#
# Поиск устойчив к символу GS (ASCII 29): он удаляется из искомого и из хранимого кода
# ТОЛЬКО на время сравнения — так найдётся и код, отсканированный в поле поиска (где GS мог
# не появиться), и код, скопированный из таблицы с пометкой [GS]. В данных ничего не меняется.
# Регистр букв при поиске по коду учитывается (серийные номера КМ чувствительны к регистру).
# ============================================================
from .db import conn, read_lock

GS = chr(29)

# Встроенная lower() в SQLite приводит к нижнему регистру только латиницу — для названий столов
# вроде «Стол 5» нужна своя функция, понимающая кириллицу.
conn.create_function("py_lower", 1, lambda v: v.lower() if isinstance(v, str) else v, deterministic=True)

_BASE = """
FROM scan_log l
LEFT JOIN items i  ON i.km_code = l.code AND l.result = 'ok'
LEFT JOIN kits  ki ON ki.id = i.kit_id
LEFT JOIN kits  ka ON ka.km_agg_code = l.code AND l.result = 'ok'
LEFT JOIN boxes bx ON bx.id = COALESCE(ki.box_id, ka.box_id)
LEFT JOIN boxes bb ON bb.km_box_code = l.code AND l.result = 'ok'
"""


def clean_query(q):
    return (q or '').replace('[GS]', '').replace(GS, '').strip(' \t\r\n')


def search_codes(q='', only_errors=False, limit=200):
    needle = clean_query(q)[:300]
    where, params = [], []
    if only_errors:
        where.append("l.result = 'error'")
    if needle:
        cond = ("(instr(replace(l.code, char(29), ''), ?) > 0 "
                "OR instr(py_lower(l.station_id), ?) > 0")
        params += [needle, needle.lower()]
        if needle.isascii() and needle.isdigit() and len(needle) <= 9:
            cond += " OR COALESCE(ki.kit_no, ka.kit_no) = ?"
            params.append(int(needle))
        cond += ")"
        where.append(cond)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit), 1000))

    with read_lock():
        total = conn.execute(f"SELECT COUNT(*) {_BASE} {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT l.id, l.created_at, l.station_id, l.code, l.code_type, l.result, l.message, "
            "COALESCE(ki.kit_no, ka.kit_no) AS kit_no, COALESCE(ki.status, ka.status) AS kit_status, "
            "COALESCE(bx.km_box_code, bb.km_box_code) AS box_code "
            f"{_BASE} {where_sql} ORDER BY l.id DESC LIMIT {limit}", params).fetchall()
    return {'total': total, 'rows': [dict(r) for r in rows]}
