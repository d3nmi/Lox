# ============================================================
# Расширенный отчёт: SQL-представление (VIEW) report_view и его выгрузка в CSV.
#
# VIEW — это «готовая таблица», которая всегда собирается из рабочих данных
# (items, kits, boxes) сама. Ничего отдельно хранить и обновлять не нужно, поэтому
# данные в ней не могут разойтись с основными. Открыть её можно любым просмотрщиком
# SQLite (например DB Browser for SQLite) — выглядит и читается как обычная таблица.
#
# Столбцы (имена в БД — латиницей, в CSV — русские заголовки):
#   kit_agg_code  Код идентификации набора   (агрегационный код набора)
#   kit_no        Номер набора               (сквозной номер по порядку закрытия)
#   item_name     Наименование товара
#   km_code       Код маркировки             (КМ товара, без изменений, включая GS)
#   box_dtv_code  ОС: номер короба DTV…      (пусто, пока набор не попал в короб)
#   assembled_at  Дата сборки                (момент закрытия набора, местное время)
#   station_id    Название станции           (стол, на котором собран набор)
# В отчёт входят только товары с КМ из ЗАКРЫТЫХ наборов (как и в основном отчёте).
# ============================================================
from .db import conn, read_lock, lock

VIEW_SQL = """
CREATE VIEW report_view AS
SELECT k.km_agg_code  AS kit_agg_code,
       k.kit_no       AS kit_no,
       i.item_name    AS item_name,
       i.km_code      AS km_code,
       b.km_box_code  AS box_dtv_code,
       k.closed_at    AS assembled_at,
       k.station_id   AS station_id,
       i.id           AS item_id          -- служебный номер строки (порядок сканирования), в CSV не выводится
FROM items i
JOIN kits k ON k.id = i.kit_id AND k.status = 'closed'
LEFT JOIN boxes b ON b.id = k.box_id
WHERE i.marked = 1
"""

HEADERS = ['Код идентификации набора', 'Номер набора', 'Наименование товара',
           'Код маркировки', 'ОС (номер короба DTV)', 'Дата сборки', 'Название станции']
_COLS = ['kit_agg_code', 'kit_no', 'item_name', 'km_code', 'box_dtv_code', 'assembled_at', 'station_id']  # столбцы отчёта (без item_id)


def ensure_report_view():
    """Вызывается при старте (после ensure_box_schema — нужен столбец kits.kit_no).
    Пересоздаётся каждый раз: данные не хранит, поэтому это безопасно."""
    with lock:
        conn.execute("DROP VIEW IF EXISTS report_view")
        conn.execute(VIEW_SQL)


def full_report_rows(limit=None):
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    with read_lock():
        rows = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM report_view ORDER BY kit_no DESC, item_id DESC {limit_sql}"
        ).fetchall()
    return [dict(r) for r in rows]


def _esc(val):
    if val is None:
        return ''
    s = str(val)
    if any(c in s for c in (';', '"', '\n', '\r')):
        return '"' + s.replace('"', '""') + '"'
    return s


def _excel(val):
    """Режим «для Excel»: чисто цифровой код пишется как ="…", чтобы Excel не сделал из него число."""
    if val is None:
        return ''
    s = str(val)
    if s.isascii() and s.isdigit():
        return '="' + s + '"'
    return _esc(s)


def full_report_data():
    """Строки расширенного отчёта в хронологическом порядке (по номеру набора). Общий источник для CSV и XLSX."""
    with read_lock():
        rows = conn.execute(
            f"SELECT {', '.join(_COLS)} FROM report_view ORDER BY kit_no, item_id"
        ).fetchall()
    return [dict(r) for r in rows]


def full_report_csv(excel=False):
    """Выгрузка в хронологическом порядке закрытия наборов. КМ записываются без изменений."""
    rows = full_report_data()
    cell_km = _excel if excel else _esc
    lines = [';'.join(_esc(h) for h in HEADERS)]
    for r in rows:
        lines.append(';'.join((
            cell_km(r['kit_agg_code']), _esc(r['kit_no']), _esc(r['item_name']), cell_km(r['km_code']),
            _esc(r['box_dtv_code']), _esc(r['assembled_at']), _esc(r['station_id']),
        )))
    return '\ufeff' + '\r\n'.join(lines)   # BOM — чтобы Excel правильно показал кириллицу
