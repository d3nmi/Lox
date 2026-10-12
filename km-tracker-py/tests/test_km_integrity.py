"""
Тесты целостности кодов маркировки (КМ): строка должна пройти весь путь
клиент -> API -> сервер -> SQLite -> отчёт/CSV без потери и изменения символов,
включая GS (ASCII 29).

Запуск из папки km-tracker-py:
    python -m unittest tests.test_km_integrity -v
Тесты используют ВРЕМЕННУЮ базу данных — рабочая БД (data/km.db) не затрагивается.
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import logging
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="km_test_")
os.environ["KM_DATA_DIR"] = _tmp
os.environ["KM_DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["KM_PASS_FILE"] = os.path.join(_tmp, "pass.txt")   # пароль для тестов — временный файл

from fastapi.testclient import TestClient   # noqa: E402
import app.main as main                      # noqa: E402  (создаёт временную БД и справочник)
from app.db import conn                      # noqa: E402
from app import validation, report           # noqa: E402

logging.getLogger('httpx').setLevel(logging.WARNING)
GS = chr(29)
# Синтетические строки из задания ([GS] -> chr(29)); это НЕ подтверждённые действительные КМ
KM1 = "0103253581768655215UZV'OEIGcUVj[GS]91EE12[GS]92cIAa6AcgQWAHqnLKEdL6Zl3vlB6Xws+TdROynLZ5Q2A=".replace("[GS]", GS)
KM2 = "0103253581735374215BaPGG;G(KVpO[GS]91EE12[GS]92fyrmOb72jfecscKSFf2xJBXNzK5kQJ9wgahSzy29zL8=".replace("[GS]", GS)
# Набор «трудных» символов (в т.ч. кавычка, точка с запятой, слэши, знаки равенства) и GS
KM_SPECIAL = '010325358176865521!"%&\'()*+,-./:;<=>?_\\=' + GS + '91EE12' + GS + '92a/b+c==x'
# GS в самом конце кода
KM_TRAILING_GS = "0103253581735374215TRAIL9" + GS
AGG_A = "01{sku}21AGGA" + "[GS]91EE12[GS]92aggSig+/=".replace("[GS]", GS)
AGG_B = "01{sku}21AGGB;\"" + GS


def gs_positions(s):
    return [i for i, ch in enumerate(s) if ch == GS]


def stored(table_col_sql, params=()):
    """-> (значение, его байты UTF-8 из SQLite, длина в символах по версии SQLite)"""
    r = conn.execute(table_col_sql, params).fetchone()
    return r[0], bytes(r[1]), r[2]


class KmIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        # справочник: набор, где есть оба товара из примеров
        cls.tpl = None
        for t in conn.execute("SELECT * FROM kit_templates ORDER BY id").fetchall():
            skus = {i["item_sku"] for i in conn.execute(
                "SELECT item_sku FROM kit_template_items WHERE kit_template_id=?", (t["id"],)).fetchall()}
            if {"03253581768655", "03253581735374"} <= skus:
                cls.tpl = t
                break
        assert cls.tpl, "в справочнике нет набора с GTIN 03253581768655 и 03253581735374"
        cls.slots = conn.execute(
            "SELECT * FROM kit_template_items WHERE kit_template_id=? ORDER BY id", (cls.tpl["id"],)).fetchall()
        cls.kit_sku = cls.tpl["kit_sku"] or "09999999999990"
        # перехват: что РЕАЛЬНО получил сервер
        cls.received = []
        cls._orig = main.process_scan

        def spy(station, raw):
            cls.received.append(raw)
            return cls._orig(station, raw)
        main.process_scan = spy

    @classmethod
    def tearDownClass(cls):
        main.process_scan = cls._orig
        conn.close()
        shutil.rmtree(_tmp, ignore_errors=True)

    # ---- вспомогательное ----
    def scan(self, station, code):
        r = self.client.post(f"/api/stations/{station}/scan", json={"code": code})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def other_codes(self, first_two_skus, tag):
        """Коды остальных позиций набора (КМ товаров с серийниками и штрихкоды упаковки)."""
        out = []
        for s in self.slots:
            if s["item_sku"] in first_two_skus:
                continue
            if s["marked"]:
                out.append(f"01{s['item_sku']}21{tag}{s['id']}")
            else:
                out.append(s["item_sku"].lstrip("0"))
        return out

    def assemble(self, station, km_a, km_b, agg, tag):
        """Собирает набор целиком: два заданных КМ + остальной состав + агрегационный код."""
        codes = [km_a, km_b] + self.other_codes({"03253581768655", "03253581735374"}, tag) + [agg.format(sku=self.kit_sku)]
        for c in codes:
            res = self.scan(station, c)
            self.assertEqual(res["result"], "ok", f"скан отклонён: {res['message']!r}")
        return codes

    @classmethod
    def csv_rows(cls, content_bytes):
        text = content_bytes.decode("utf-8-sig")
        # ВАЖНО: читаем csv через StringIO(newline=''), а не text.splitlines() —
        # splitlines() в Python режет строку и по символу GS.
        return list(csv.reader(io.StringIO(text, newline=""), delimiter=";"))

    # ---- тесты ----
    def test_01_strip_scan_keeps_gs_on_edges(self):
        self.assertEqual(validation.strip_scan(GS + "abc" + GS), GS + "abc" + GS)
        self.assertEqual(validation.strip_scan("  \tabc" + GS + "\r\n"), "abc" + GS)
        self.assertEqual(validation.strip_scan(None), "")
        # контроль: обычный str.strip() GS бы отрезал — именно эту ошибку исправляем
        self.assertNotEqual((GS + "abc").strip(), GS + "abc")

    def test_02_client_level_via_node(self):
        try:
            subprocess.run(["node", "--version"], capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("Node.js не установлен — клиентская часть не проверена")
        samples = [KM1, KM2, KM_SPECIAL, KM_TRAILING_GS, GS + "x", "  " + KM1 + "\r\n"]
        p = subprocess.run(["node", str(ROOT / "tests" / "client_km_check.js")],
                           input=json.dumps(samples), capture_output=True, text=True, encoding="utf-8", check=True)
        res = json.loads(p.stdout)
        expected = [KM1, KM2, KM_SPECIAL, KM_TRAILING_GS, GS + "x", KM1]   # пробелы/CRLF по краям снимаются, GS — нет
        for got, exp in zip(res, expected):
            self.assertEqual(got["normalized"], exp)
            self.assertEqual(got["body_roundtrip"], exp)

    def test_03_full_path_examples_and_special_chars(self):
        n_before = len(self.received)
        codes_a = self.assemble("Стол A", KM1, KM2, AGG_A, "SA")
        codes_b = self.assemble("Стол B", KM_SPECIAL, KM_TRAILING_GS, AGG_B, "SB")

        # уровень 3: что получил сервер — ровно то, что отправили
        sent = codes_a + codes_b
        self.assertEqual(self.received[n_before:], sent)

        report_lines = []
        for original in (KM1, KM2, KM_SPECIAL, KM_TRAILING_GS, AGG_A.format(sku=self.kit_sku), AGG_B.format(sku=self.kit_sku)):
            table, col = ("kits", "km_agg_code") if original.startswith(("01" + self.kit_sku,)) and ("AGG" in original) else ("items", "km_code")
            # уровень 4-5: значение в SQLite и после чтения
            val, raw_bytes, length = stored(
                f"SELECT {col}, CAST({col} AS BLOB), length({col}) FROM {table} WHERE {col}=?", (original,))
            self.assertEqual(val, original)
            self.assertEqual(raw_bytes, original.encode("utf-8"))
            self.assertEqual(length, len(original))
            self.assertEqual(gs_positions(val), gs_positions(original))
            self.assertNotIn("\\u001d", val.lower())
            self.assertNotIn("\\x1d", val.lower())
            # журнал сканов и таблица использованных кодов
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scan_log WHERE code=?", (original,)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM used_codes WHERE code=?", (original,)).fetchone()[0], 1)
            report_lines.append((original, len(original), gs_positions(original), len(original.encode("utf-8"))))

        # уровень 6: отчёт (CSV) и его повторное чтение
        r = self.client.get("/api/export.csv")
        self.assertEqual(r.status_code, 200)
        raw = r.content
        self.assertEqual(raw.count(b"\x1d"), self._expected_gs_in_report(), "число байтов GS в файле")
        self.assertNotIn(b"\\u001d", raw)
        self.assertNotIn(b"[GS]", raw)
        rows = self.csv_rows(raw)
        km_col = [row[3] for row in rows[1:]]
        for original in (KM1, KM2, KM_SPECIAL, KM_TRAILING_GS):
            self.assertIn(original, km_col, "КМ в CSV изменён или потерян")
            self.assertEqual(gs_positions(rows[1:][km_col.index(original)][3]), gs_positions(original))
        agg_col = {row[0] for row in rows[1:]}
        self.assertIn(AGG_A.format(sku=self.kit_sku), agg_col)
        self.assertIn(AGG_B.format(sku=self.kit_sku), agg_col)

        # то же через предпросмотр (JSON) — то, что видит страница отчёта
        prev = self.client.get("/api/report/preview?limit=500").json()
        self.assertIn(KM1, [x["km_code"] for x in prev["rows"]])
        self.assertIn(KM_TRAILING_GS, [x["km_code"] for x in prev["rows"]])

        # отдельная выгрузка набор/вложение/короб
        r2 = self.client.get("/api/export-data.csv")
        rows2 = self.csv_rows(r2.content)
        self.assertIn(KM2, [row[1] for row in rows2[1:]])
        self.assertIn(AGG_A.format(sku=self.kit_sku), [row[0] for row in rows2[1:]])

        # выгрузка «для Excel»: КМ не меняются (в них есть буквы), числовые — защищены
        r3 = self.client.get("/api/export.csv?excel=1")
        rows3 = self.csv_rows(r3.content)
        self.assertIn(KM1, [row[3] for row in rows3[1:]])
        self.assertEqual(report._excel_cell("0104630111270571210000123"), '="0104630111270571210000123"')
        self.assertEqual(report._excel_cell(KM1), report._csv_escape(KM1))

        print("\n--- Сводка по КМ (длина, позиции GS, байт UTF-8) ---")
        for o, n, pos, nb in report_lines:
            print(f"{o!r:.70}…  длина={n}  GS на позициях {pos}  байт={nb}")

    def _expected_gs_in_report(self):
        """Сколько символов GS должно быть в CSV: по тем же строкам, что попадают в отчёт."""
        total = 0
        for agg, km in conn.execute(
                "SELECT k.km_agg_code, i.km_code FROM items i JOIN kits k ON k.id=i.kit_id AND k.status='closed' WHERE i.marked=1"):
            total += agg.count(GS) + km.count(GS)
        return total

    def test_04_duplicate_and_case_sensitivity(self):
        res = self.scan("Стол A", KM1)          # КМ1 уже принят в test_03
        self.assertEqual(res["result"], "error")
        self.assertIn("дубль", res["message"])
        self.assertNotIn(GS, res["message"], "в тексте сообщения не должно быть невидимого GS")
        self.assertIn("[GS]", res["message"])
        # регистр различает коды: версия с другим регистром — НЕ тот же код
        swapped = KM1.swapcase()
        self.assertNotEqual(swapped, KM1)
        n = conn.execute("SELECT COUNT(*) FROM used_codes WHERE code=?", (swapped,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_05_empty_and_too_long(self):
        res = self.scan("Стол C", "   ")
        self.assertEqual(res["result"], "error")
        res = self.scan("Стол C", "1" * 500)
        self.assertEqual(res["result"], "error")


    def test_06_full_report_view(self):
        """Расширенный отчёт: SQL-представление report_view и CSV с 7 столбцами."""
        import re
        cols = [r[1] for r in conn.execute("PRAGMA table_info(report_view)").fetchall()]
        self.assertEqual(cols, ["kit_agg_code", "kit_no", "item_name", "km_code", "box_dtv_code", "assembled_at", "station_id"])
        n_view = conn.execute("SELECT COUNT(*) FROM report_view").fetchone()[0]
        self.assertEqual(n_view, report.report_count() if hasattr(report, "report_count") else n_view)
        # до сканирования короба — столбец ОС пуст
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM report_view WHERE box_dtv_code IS NOT NULL").fetchone()[0], 0)
        # сканируем короб DTV… — он забирает закрытые наборы
        box = "DTV0003111664"
        res = self.scan("Стол A", box)
        self.assertEqual(res["result"], "ok", res["message"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM report_view WHERE box_dtv_code IS NULL").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(DISTINCT box_dtv_code) FROM report_view").fetchone()[0], 1)

        r = self.client.get("/api/export-full.csv")
        self.assertEqual(r.status_code, 200)
        rows = self.csv_rows(r.content)
        self.assertEqual(rows[0], ["Код идентификации набора", "Номер набора", "Наименование товара",
                                   "Код маркировки", "ОС (номер короба DTV)", "Дата сборки", "Название станции"])
        body = rows[1:]
        self.assertEqual(len(body), n_view)
        for row in body:
            self.assertEqual(len(row), 7)
            self.assertEqual(row[4], box)
            self.assertRegex(row[5], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
            self.assertIn(row[6], ("Стол A", "Стол B"))
        km_col = [row[3] for row in body]
        for original in (KM1, KM2, KM_SPECIAL, KM_TRAILING_GS):
            self.assertIn(original, km_col, "КМ в расширенном CSV изменён или потерян")
        by_km = {row[3]: row for row in body}
        self.assertEqual(by_km[KM1][6], "Стол A")
        self.assertEqual(by_km[KM_SPECIAL][6], "Стол B")
        # Excel-вариант: КМ с буквами не меняются
        rows_x = self.csv_rows(self.client.get("/api/export-full.csv?excel=1").content)
        self.assertIn(KM1, [row[3] for row in rows_x[1:]])


    def test_07_require_box_needs_password(self):
        """Галочка «Код короба» меняется только с верным паролем из файла pass.txt."""
        url = "/api/stations/Стол P/settings"
        pass_file = Path(os.environ["KM_PASS_FILE"])
        self.assertTrue(pass_file.exists(), "pass.txt должен создаваться автоматически")
        pass_file.write_text("# комментарий\n\nсекрет-42\n", encoding="utf-8-sig")   # BOM и перевод строки, как из Блокнота

        def require_box():
            row = conn.execute("SELECT require_box FROM station_settings WHERE station_id=?", ("Стол P",)).fetchone()
            return bool(row and row["require_box"])

        r = self.client.post(url, json={"require_box": True})                       # без пароля
        self.assertEqual(r.json()["result"], "error")
        r = self.client.post(url, json={"require_box": True, "password": "1234"})   # неверный
        self.assertEqual(r.json()["result"], "error")
        self.assertFalse(require_box())
        r = self.client.post(url, json={"require_box": True, "password": "секрет-42"})
        self.assertEqual(r.json()["result"], "ok", r.text)
        self.assertTrue(require_box())
        r = self.client.post(url, json={"require_box": False, "password": "неверно"})   # выключить без пароля нельзя
        self.assertEqual(r.json()["result"], "error")
        self.assertTrue(require_box())
        r = self.client.post(url, json={"require_box": False, "password": "секрет-42"})
        self.assertTrue(r.json()["result"] == "ok" and not require_box())
        # пароль не отдаётся клиенту ни в каком виде
        self.assertNotIn("секрет-42", r.text)
        # пароль меняется правкой файла без перезапуска сервера
        pass_file.write_text("новый\n", encoding="utf-8")
        self.assertEqual(self.client.post(url, json={"require_box": True, "password": "секрет-42"}).json()["result"], "error")
        self.assertEqual(self.client.post(url, json={"require_box": True, "password": "новый"}).json()["result"], "ok")
        # файл пуст/удалён — доступ закрыт, а не открыт
        pass_file.write_text("# только комментарий\n", encoding="utf-8")
        self.assertEqual(self.client.post(url, json={"require_box": False, "password": ""}).json()["result"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
