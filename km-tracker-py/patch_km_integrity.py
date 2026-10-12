"""
Исправления целостности КМ в app/validation.py и app/report.py.
Запуск (из папки km-tracker-py):   python patch_km_integrity.py
Перед правкой рядом сохраняются копии: validation.py.bak, report.py.bak.
Скрипт можно запускать повторно — второй раз он ничего не изменит.

Что исправляется:
  1. validation.py: Python-функция str.strip() считала символ GS (ASCII 29) пробелом и
     отрезала его на краю кода. Теперь по краям убираются только пробелы/табуляция/CR/LF.
  2. validation.py: регулярные выражения принимали не-ASCII цифры (например арабские) — добавлен re.ASCII.
  3. validation.py: в тексте сообщений об ошибках GS показывается как [GS]
     (в базу и журнал кода сообщение идёт отдельно, сам код остаётся исходным).
  4. report.py: добавлена выгрузка «для Excel» (числовые коды как текст, чтобы Excel не искажал их).
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
V = ROOT / "app" / "validation.py"
R = ROOT / "app" / "report.py"
MARK_V = "# --- km-integrity patch ---"
MARK_R = "# --- km-integrity patch (excel) ---"


def need(text, old, label):
    if old not in text:
        print(f"ОШИБКА: не найден фрагмент для правки «{label}». Файл отличается от ожидаемого — ничего не изменено.")
        sys.exit(1)


def patch_validation():
    s = V.read_text(encoding="utf-8")
    if MARK_V in s:
        print("validation.py: уже исправлен")
        return
    # 1. проверяем все якоря заранее, чтобы не оставить файл наполовину изменённым
    anchors = [
        ("GTIN_RE = re.compile(r'^01(\\d{14})')", "GTIN_RE"),
        ("PLAIN_RE = re.compile(r'^\\d{13,14}$')", "PLAIN_RE"),
        ("BOX_CODE_RE = re.compile(r'^DTV\\d{10}$')", "BOX_CODE_RE"),
        ("FAKE_AGG_GTIN = '09999999999990'", "FAKE_AGG_GTIN"),
        ("    code = (raw_code or '').strip()", "strip в process_scan"),
        ("f'Код уже был отсканирован ранее (дубль): {code}'", "сообщение о дубле"),
        ("""f'Код "{code[:24]}" не распознан""", "сообщение «не распознан»"),
    ]
    for old, label in anchors:
        need(s, old, label)

    s = s.replace("GTIN_RE = re.compile(r'^01(\\d{14})')", "GTIN_RE = re.compile(r'^01(\\d{14})', re.ASCII)")
    s = s.replace("PLAIN_RE = re.compile(r'^\\d{13,14}$')", "PLAIN_RE = re.compile(r'^\\d{13,14}$', re.ASCII)")
    s = s.replace("BOX_CODE_RE = re.compile(r'^DTV\\d{10}$')", "BOX_CODE_RE = re.compile(r'^DTV\\d{10}$', re.ASCII)")
    helpers = (
        "FAKE_AGG_GTIN = '09999999999990'\n\n"
        f"{MARK_V}\n"
        "GS = chr(29)   # управляющий символ GS (ASCII 29) — часть КМ, между полями GS1\n\n\n"
        "def strip_scan(raw):\n"
        "    \"\"\"Убирает по краям ТОЛЬКО пробелы, табуляцию, CR и LF (их добавляет сканер/ввод).\n"
        "    Нельзя использовать str.strip() без аргументов: в Python он считает пробелом и GS (ASCII 29),\n"
        "    и отрезал бы его на краю кода.\"\"\"\n"
        "    return (raw or '').strip(' \\t\\r\\n')\n\n\n"
        "def _vis(s):\n"
        "    \"\"\"Только для текста сообщений на экране: настоящий GS показывается как [GS].\"\"\"\n"
        "    return (s or '').replace(GS, '[GS]')\n"
    )
    s = s.replace("FAKE_AGG_GTIN = '09999999999990'", helpers, 1)
    s = s.replace("    code = (raw_code or '').strip()", "    code = strip_scan(raw_code)")
    s = s.replace("f'Код уже был отсканирован ранее (дубль): {code}'", "f'Код уже был отсканирован ранее (дубль): {_vis(code)}'")
    s = s.replace("""f'Код "{code[:24]}" не распознан""", """f'Код "{_vis(code)[:24]}" не распознан""")
    shutil.copyfile(V, V.with_suffix(".py.bak"))
    V.write_text(s, encoding="utf-8")
    print("validation.py: исправлен")


EXCEL_BLOCK = f'''

{MARK_R}
def _excel_cell(val):
    """Для выгрузки «для Excel»: чисто цифровой код записывается как ="..." — так Excel
    оставит его текстом (иначе превратит в число: потеряет ведущие нули и точность).
    Остальные значения — как в обычной выгрузке, без изменений."""
    if val is None:
        return ''
    s = str(val)
    if s.isascii() and s.isdigit():
        return '="' + s + '"'
    return _csv_escape(s)


def export_csv_excel():
    rows = list(reversed(report_rows(None)))
    header = ['Код идентификации набора', 'Номер набора', 'Наименование товара', 'Код маркировки']
    lines = [';'.join(_csv_escape(h) for h in header)]
    for r in rows:
        lines.append(';'.join((_excel_cell(r['kit_agg_code']), _csv_escape(r['kit_no']),
                               _csv_escape(r['item_name']), _excel_cell(r['km_code']))))
    return '\\ufeff' + '\\r\\n'.join(lines)


def export_data_csv_excel():
    rows = list(reversed(export_data_rows(None)))
    header = ['Код набора', 'Код вложения', 'Код короба']
    lines = [';'.join(_csv_escape(h) for h in header)]
    for r in rows:
        lines.append(';'.join(_excel_cell(v) for v in (r['kit_agg_code'], r['item_km_code'], r['box_km_code'])))
    return '\\ufeff' + '\\r\\n'.join(lines)
'''


def patch_report():
    s = R.read_text(encoding="utf-8")
    if MARK_R in s:
        print("report.py: уже исправлен")
        return
    for fn in ("def _csv_escape", "def export_csv", "def export_data_csv", "def report_rows", "def export_data_rows"):
        need(s, fn, fn)
    shutil.copyfile(R, R.with_suffix(".py.bak"))
    R.write_text(s.rstrip("\n") + "\n" + EXCEL_BLOCK, encoding="utf-8")
    print("report.py: исправлен (добавлена выгрузка для Excel)")


if __name__ == "__main__":
    for f in (V, R):
        if not f.exists():
            print(f"Не найден файл {f}. Запускайте скрипт из папки km-tracker-py.")
            sys.exit(1)
    patch_validation()
    patch_report()
    print("Готово.")
