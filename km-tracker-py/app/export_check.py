# ============================================================
# Проверка согласованности данных и выгрузок (CSV) — только ЧТЕНИЕ.
# Сравнивает то, что лежит в базе, с тем, что попадает в отчёты, и ищет типичные поломки:
# неполные наборы, дыры в нумерации, коды без GS, «чужие» управляющие символы, расхождения таблиц,
# а также разбирает готовые CSV обратно и сверяет их с базой посимвольно.
# Результат: problems — ошибки (данные/выгрузка неверны), warnings — на что обратить внимание.
# ============================================================
import csv
import io
import re

from .db import conn, read_lock
from . import report, report_view

GS = chr(29)
_GLUED_RE = re.compile(r'^01\d{14}21[\x21-\x7e]{1,20}91[\x21-\x7e]{4}92(?:[\x21-\x7e]{88}|[\x21-\x7e]{44})$', re.ASCII)
_BAD_CTRL_RE = re.compile(r'[\x00-\x08\x0a-\x1c\x1e-\x1f\x7f]')   # управляющие символы, кроме GS (0x1D) и табуляции
MAX_EXAMPLES = 5


def _vis(s):
    s = (s or '').replace(GS, '[GS]')
    return s if len(s) <= 40 else s[:20] + '…' + s[-12:]


def _parse_csv(text):
    # splitlines() режет строку по символу GS, поэтому читаем через StringIO(newline='')
    return list(csv.reader(io.StringIO(text.lstrip('\ufeff'), newline=''), delimiter=';'))


def _unwrap_excel(cell):
    m = re.fullmatch(r'="(\d+)"', cell)
    return m.group(1) if m else cell


def check_export_consistency():
    problems, warnings = [], []

    def examples(items):
        shown = ', '.join(_vis(x) for x in items[:MAX_EXAMPLES])
        return shown + (f' … (всего {len(items)})' if len(items) > MAX_EXAMPLES else '')

    with read_lock():
        kits = conn.execute("SELECT * FROM kits WHERE status='closed' ORDER BY id").fetchall()
        item_counts = {r["kit_id"]: (r["n"], r["marked"] or 0) for r in conn.execute(
            "SELECT kit_id, COUNT(*) AS n, SUM(CASE WHEN marked=1 THEN 1 ELSE 0 END) AS marked FROM items GROUP BY kit_id")}
        db_pairs = [(r["km_agg_code"], r["km_code"]) for r in conn.execute(
            "SELECT k.km_agg_code, i.km_code FROM items i JOIN kits k ON k.id=i.kit_id AND k.status='closed' WHERE i.marked=1")]
        export_pairs = [(r["km_agg_code"], r["km_code"]) for r in conn.execute("SELECT km_agg_code, km_code FROM export_data")]
        view_count = conn.execute("SELECT COUNT(*) FROM report_view").fetchone()[0]
        used = {r["code"]: r["code_type"] for r in conn.execute("SELECT code, code_type FROM used_codes")}
        box_mismatch = conn.execute(
            "SELECT COUNT(*) FROM export_data ed JOIN kits k ON k.id=ed.kit_id LEFT JOIN boxes b ON b.id=k.box_id "
            "WHERE COALESCE(ed.km_box_code,'') <> COALESCE(b.km_box_code,'')").fetchone()[0]
        view_box_mismatch = conn.execute(
            "SELECT COUNT(*) FROM report_view v JOIN kits k ON k.km_agg_code=v.kit_agg_code LEFT JOIN boxes b ON b.id=k.box_id "
            "WHERE COALESCE(v.box_dtv_code,'') <> COALESCE(b.km_box_code,'')").fetchone()[0]
        open_kits = conn.execute("SELECT COUNT(*) FROM kits WHERE status='open'").fetchone()[0]
        std_rows = list(reversed(report.report_rows(None)))
        std_csv = report.export_csv()
        full_csv = report_view.full_report_csv(False)
        full_rows = report_view.full_report_data()
        excel_csv = report.export_csv_excel() if hasattr(report, 'export_csv_excel') else None

    # --- 1. наборы: полнота и номера ---
    incomplete = [k["km_agg_code"] for k in kits
                  if k["items_count"] != item_counts.get(k["id"], (0, 0))[0] or k["items_count"] < k["items_required"]]
    if incomplete:
        problems.append(f'Закрытые наборы с неполным составом: {examples(incomplete)}')
    no_agg = [k["id"] for k in kits if not k["km_agg_code"]]
    if no_agg:
        problems.append(f'Закрытые наборы без агрегационного кода (id: {no_agg[:MAX_EXAMPLES]})')
    numbers = [k["kit_no"] for k in kits]
    if any(n is None for n in numbers):
        problems.append('У закрытых наборов есть пустой «Номер набора»')
    nums = [n for n in numbers if n is not None]
    dups = sorted({n for n in nums if nums.count(n) > 1})
    if dups:
        problems.append(f'Повторяющиеся номера наборов: {dups[:MAX_EXAMPLES]}')
    elif nums and sorted(nums) != list(range(1, len(nums) + 1)):
        warnings.append('В нумерации наборов есть пропуски (номера идут не подряд 1…N) — возможно, данные удаляли вручную')

    # --- 2. отчёт = база = выгрузочная таблица ---
    if len(std_rows) != len(db_pairs):
        problems.append(f'Строк в отчёте {len(std_rows)}, а товаров с КМ в закрытых наборах {len(db_pairs)}')
    rep_pairs = sorted((r['kit_agg_code'], r['km_code']) for r in std_rows)
    if rep_pairs != sorted(db_pairs):
        problems.append('Состав строк отчёта не совпадает с данными базы (пары «код набора — КМ»)')
    if sorted(export_pairs) != sorted(db_pairs):
        problems.append(f'Таблица export_data ({len(export_pairs)} строк) не совпадает с базой ({len(db_pairs)} строк)')
    if view_count != len(db_pairs):
        problems.append(f'В report_view {view_count} строк, ожидалось {len(db_pairs)}')
    if box_mismatch or view_box_mismatch:
        problems.append(f'Номер короба в выгрузках не совпадает с коробом набора (export_data: {box_mismatch}, report_view: {view_box_mismatch})')

    # --- 3. использованные коды ---
    unused_items = [km for _, km in db_pairs if used.get(km) != 'item']
    if unused_items:
        problems.append(f'КМ товаров, которых нет в таблице использованных кодов (защита от дублей не сработает): {examples(unused_items)}')
    unused_agg = [k["km_agg_code"] for k in kits if k["km_agg_code"] and used.get(k["km_agg_code"]) != 'kit_agg']
    if unused_agg:
        problems.append(f'Агрегационные коды, которых нет в таблице использованных кодов: {examples(unused_agg)}')

    # --- 4. сами коды: GS, управляющие символы, пробелы ---
    all_codes = [km for _, km in db_pairs] + [k["km_agg_code"] for k in kits if k["km_agg_code"]]
    glued = [c for c in all_codes if GS not in c and _GLUED_RE.match(c)]
    if glued:
        warnings.append(f'Коды, похожие на КМ с полями 91/92, но БЕЗ символа GS (GS мог потеряться при сканировании): {len(glued)} шт., например {examples(glued)}')
    ctrl = [c for c in all_codes if _BAD_CTRL_RE.search(c)]
    if ctrl:
        problems.append(f'Коды с посторонними управляющими символами (кроме GS): {examples(ctrl)}')
    spaces = [c for c in all_codes if c != c.strip(' \t\r\n')]
    if spaces:
        problems.append(f'Коды с пробелами/переводами строки по краям: {examples(spaces)}')
    hashed = [km for _, km in db_pairs if re.search(r'#\d+$', km)]
    if hashed:
        problems.append(f'Служебный суффикс «#номер» у товаров с КМ: {examples(hashed)}')

    # --- 5. выгрузки: разбор готовых CSV и посимвольное сравнение с базой ---
    parsed = _parse_csv(std_csv)
    expected = [[r['kit_agg_code'], str(r['kit_no']) if r['kit_no'] is not None else '', r['item_name'], r['km_code']] for r in std_rows]
    if parsed[0] != ['Код идентификации набора', 'Номер набора', 'Наименование товара', 'Код маркировки']:
        problems.append('Основной CSV: неожиданные заголовки')
    if parsed[1:] != expected:
        problems.append('Основной CSV после разбора не совпадает с данными базы')
    if excel_csv is not None:
        pe = [[_unwrap_excel(c) for c in row] for row in _parse_csv(excel_csv)[1:]]
        if pe != expected:
            problems.append('CSV «для Excel» после разбора не совпадает с данными базы')
    pf = _parse_csv(full_csv)
    exp_full = [[r['kit_agg_code'], str(r['kit_no']), r['item_name'], r['km_code'], r['box_dtv_code'] or '',
                 r['assembled_at'] or '', r['station_id']] for r in full_rows]
    if pf[0] != report_view.HEADERS:
        problems.append('Расширенный CSV: неожиданные заголовки')
    if pf[1:] != exp_full:
        problems.append('Расширенный CSV после разбора не совпадает с данными базы')

    # --- 6. порядок строк ---
    kit_nos = [r['kit_no'] for r in std_rows if r['kit_no'] is not None]
    if kit_nos != sorted(kit_nos):
        warnings.append('В основном отчёте строки идут в порядке открытия наборов, а не по номеру набора (номера могут выглядеть «вразнобой», '
                        'если наборы закрывались не в том порядке, в каком начинались). Расширенный отчёт отсортирован по номеру набора.')

    stats = {'closed_kits': len(kits), 'open_kits': open_kits, 'report_rows': len(std_rows),
             'codes_checked': len(all_codes), 'codes_with_gs': sum(1 for c in all_codes if GS in c)}
    return {'ok': not problems, 'problems': problems, 'warnings': warnings, 'stats': stats}
