# ============================================================
# Выгрузка в формате XLSX (Excel): один лист «Расширенный» (7 столбцов, как расширенный CSV). Строки берутся из ТОЙ ЖЕ
# функции, что и расширенный CSV, поэтому данные совпадают с CSV; согласованность проверяют тесты и /api/export-check.
#
# Особенности формата, которые учтены:
#   • Коды КМ и агрегационные коды записываются ТЕКСТОМ (формат «@», тип строки) — Excel не превращает их
#     в числа/даты и не принимает строки вида «=…» за формулы.
#   • В XLSX (XML 1.0) нельзя хранить управляющие символы, в том числе GS (ASCII 29). Поэтому GS записывается
#     штатным для Excel способом — последовательностью «_x001D_» (Excel и LibreOffice читают её как настоящий GS).
#     Буквальный текст вида «_xHHHH_» экранируется (_x005F_xHHHH_), чтобы его не приняли за такой символ.
#     Программы, которые не декодируют эти последовательности (например, pandas/openpyxl), покажут «_x001D_» —
#     для них есть режимы gs=tag ([GS]), gs=space, gs=none.
#   • Если нужно получить код БЕЗ риска двусмысленности — используйте CSV (там GS записан настоящим байтом 0x1D).
# ============================================================
import io
import re
from datetime import datetime

from . import report_view

GS = chr(29)
_LITERAL_ESC_RE = re.compile(r'_x([0-9A-Fa-f]{4})_')
_XML_BAD_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')    # запрещены в XML 1.0 (табуляция, \n, \r разрешены)
GS_MODES = ('raw', 'tag', 'space', 'none')


def xml_text(value, gs='raw'):
    """Текст ячейки: выбранное представление GS, экранирование «_xHHHH_» и кодирование запрещённых символов."""
    s = '' if value is None else str(value)
    if gs == 'tag':
        s = s.replace(GS, '[GS]')
    elif gs == 'space':
        s = s.replace(GS, ' ')
    elif gs == 'none':
        s = s.replace(GS, '')
    s = _LITERAL_ESC_RE.sub(lambda m: '_x005F_x' + m.group(1) + '_', s)      # сначала буквальные последовательности
    return _XML_BAD_RE.sub(lambda m: f'_x{ord(m.group(0)):04X}_', s)           # затем настоящие управляющие символы (GS)


def decode_xml_text(s):
    """Обратное преобразование (для проверок): _xHHHH_ -> символ."""
    return re.sub(r'_x([0-9A-Fa-f]{4})_', lambda m: chr(int(m.group(1), 16)), s)


def _require_openpyxl():
    try:
        import openpyxl  # noqa: F401
    except ImportError as err:
        raise RuntimeError('Для выгрузки в XLSX нужна библиотека openpyxl: выполните «pip install openpyxl».') from err


def build_xlsx_bytes(tables, gs='raw'):
    """tables: [{title, headers, rows, text_cols, widths, date_cols}] -> байты .xlsx (режим write_only — экономит память)."""
    _require_openpyxl()
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font

    wb = Workbook(write_only=True)
    bold = Font(bold=True)
    for t in tables:
        ws = wb.create_sheet(title=t['title'])
        ws.freeze_panes = 'A2'
        for i, w in enumerate(t.get('widths', []), start=1):
            ws.column_dimensions[chr(64 + i)].width = w
        ncols = len(t['headers'])
        ws.auto_filter.ref = f"A1:{chr(64 + ncols)}{max(1, len(t['rows']) + 1)}"

        head = []
        for h in t['headers']:
            c = WriteOnlyCell(ws, value=h)
            c.font = bold
            c.alignment = Alignment(wrap_text=True, vertical='center')
            head.append(c)
        ws.append(head)

        for row in t['rows']:
            out = []
            for idx, val in enumerate(row):
                if val is None or val == '':
                    out.append(None)
                elif idx in t.get('text_cols', ()):
                    c = WriteOnlyCell(ws, value=xml_text(val, gs))
                    c.data_type = 's'                 # строго текст (даже если выглядит как число или «=формула»)
                    c.number_format = '@'
                    out.append(c)
                elif idx in t.get('date_cols', ()):
                    try:
                        c = WriteOnlyCell(ws, value=datetime.strptime(str(val), '%Y-%m-%d %H:%M:%S'))
                        c.number_format = 'DD.MM.YYYY HH:MM:SS'
                        out.append(c)
                    except ValueError:
                        out.append(xml_text(val, gs))
                elif isinstance(val, int):
                    out.append(val)
                else:
                    c = WriteOnlyCell(ws, value=xml_text(val, gs))
                    c.data_type = 's'
                    out.append(c)
            ws.append(out)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_xlsx(gs='raw'):
    """XLSX с одним листом «Расширенный» (7 столбцов, строки те же, что в расширенном CSV)."""
    if gs not in GS_MODES:
        raise ValueError('gs')
    full = report_view.full_report_data()
    tables = [
        {'title': 'Расширенный',
         'headers': report_view.HEADERS,
         'rows': [[r['kit_agg_code'], r['kit_no'], r['item_name'], r['km_code'], r['box_dtv_code'], r['assembled_at'], r['station_id']] for r in full],
         'text_cols': (0, 3, 4), 'date_cols': (5,), 'widths': [60, 14, 48, 80, 20, 20, 18]},
    ]
    return build_xlsx_bytes(tables, gs)
