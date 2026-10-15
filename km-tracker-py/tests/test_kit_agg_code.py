"""
Тесты распознавания агрегационного кода набора (DM с GTIN из колонки «GTIN набора» kits.csv).
Каждый сценарий идёт в отдельном процессе на КОПИИ программы с собственным kits.csv и временной БД.
Запуск из km-tracker-py:  python -m unittest tests.test_kit_agg_code -v
"""
import csv, json, os, shutil, subprocess, sys, tempfile, textwrap, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEAD = ["Название набора", "GTIN набора", "Название товара", "GTIN товара", "Количество", "С маркировкой (КМ)"]
KIT = 'ПОДАРОЧНЫЙ НАБОР "НЕЖНОЕ КАРИТЕ"'
GOODS = [("Крем для рук", "3253581768655"), ("Крем для тела", "3253581735374"), ("Мыло", "3253581717714")]

DRIVER = textwrap.dedent('''
    import os, sys, json, logging
    sys.path.insert(0, sys.argv[1]); logging.disable(logging.CRITICAL)
    from fastapi.testclient import TestClient
    import app.main as m
    c = TestClient(m.app); GS = chr(29); out = []
    for st, code in json.load(sys.stdin):
        r = c.post(f"/api/stations/{st}/scan", json={"code": code.replace("[GS]", GS)}).json()
        out.append([r["result"], r["message"]])
    closed = m.conn.execute("SELECT kit_no, km_agg_code FROM kits WHERE status='closed' ORDER BY kit_no").fetchall()
    print("@@" + json.dumps({"r": out, "closed": [[k[0], k[1]] for k in closed]}, ensure_ascii=False))
''')


def dm(gtin, ser):
    return f"01{gtin.zfill(14)}21{ser}[GS]91EE12[GS]92sig="


def run(kit_gtin, scans, extra_rows=()):
    tmp = Path(tempfile.mkdtemp(prefix="kitagg_"))
    try:
        shutil.copytree(ROOT, tmp / "p", ignore=shutil.ignore_patterns("data", "__pycache__", "tests", ".git"))
        rows = [[KIT, kit_gtin, n, g, 1, "да"] for n, g in GOODS] + [list(r) for r in extra_rows]
        with open(tmp / "p" / "kits.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";"); w.writerow(HEAD); w.writerows(rows)
        (tmp / "drv.py").write_text(DRIVER, encoding="utf-8")
        env = dict(os.environ, KM_DATA_DIR=str(tmp / "d"), KM_PASS_FILE=str(tmp / "d" / "p.txt"))
        env.pop("KM_DB_PATH", None)
        p = subprocess.run([sys.executable, "-I", str(tmp / "drv.py"), str(tmp / "p")], input=json.dumps(scans),
                           capture_output=True, text=True, env=env, timeout=120)
        line = [l for l in p.stdout.splitlines() if l.startswith("@@")]
        assert line, p.stdout + p.stderr
        return json.loads(line[-1][2:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def goods(prefix="A", st="S1"):
    return [[st, dm(g, f"{prefix}{i}")] for i, (_, g) in enumerate(GOODS)]


class KitAggCode(unittest.TestCase):
    def test_closes_with_14_and_13_digit_kit_gtin(self):
        for kit_gtin in ("04630111270571", "4630111270571"):
            with self.subTest(kit_gtin=kit_gtin):
                res = run(kit_gtin, goods() + [["S1", dm("04630111270571", "N1")]])
                self.assertEqual(res["r"][-1][0], "ok", res["r"][-1][1])
                self.assertEqual(res["closed"][0][0], 1)
                self.assertIn("010463011127057121N1", res["closed"][0][1])   # DM сохранён в km_agg_code

    def test_incomplete_kit_not_closed_and_dm_not_burned(self):
        dmk = dm("04630111270571", "N2")
        res = run("4630111270571", goods()[:2] + [["S1", dmk], ["S1", goods()[2][1]], ["S1", dmk]])
        self.assertEqual(res["r"][2][0], "error")
        self.assertIn("ещё не собран", res["r"][2][1])
        self.assertEqual(res["r"][-1][0], "ok")      # после добора тот же DM принят — не сгорел
        self.assertEqual(len(res["closed"]), 1)

    def test_wrong_gtin_when_complete_does_not_close(self):
        res = run("4630111270571", goods() + [["S1", dm("4630111279999", "W")]])
        self.assertEqual(res["r"][-1][0], "error")
        self.assertEqual(res["closed"], [])

    def test_repeat_same_dm_is_duplicate(self):
        dmk = dm("04630111270571", "N3")
        res = run("4630111270571", goods() + [["S1", dmk], ["S1", dmk]])
        self.assertEqual(res["r"][-1][0], "error")
        self.assertIn("дубль", res["r"][-1][1])
        self.assertEqual(len(res["closed"]), 1)

    def test_kit_without_kit_gtin_still_closes(self):
        res = run("", goods() + [["S1", dm("04630111270571", "N4")]])
        self.assertEqual(res["r"][-1][0], "ok", res["r"][-1][1])

    def test_packaging_rows_raise_required_count(self):
        # как в kits.csv репозитория: упаковка тоже обязательная позиция -> после 3 товаров набор ещё не полон
        extra = [(KIT, "4630111270571", "Упаковка", "2000200741824", 1, "да")]
        res = run("4630111270571", goods() + [["S1", dm("04630111270571", "N5")]], extra)
        self.assertEqual(res["r"][-1][0], "error")
        self.assertIn("ещё не собран", res["r"][-1][1])


if __name__ == "__main__":
    unittest.main()
