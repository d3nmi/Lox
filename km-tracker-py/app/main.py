# ============================================================
# Локальный сервер учёта иерархии КМ — один склад, до 20 станций.
# Этап 3 плана реализации, порт на Python/FastAPI/SQLite.
# ============================================================
import re

from fastapi import FastAPI, HTTPException, Response, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import conn, init_db
from .validation import process_scan, get_station_state
from .control import build_tree, search_code, get_stats
from .report import export_csv, report_rows, report_count
from .backup import run_backup, schedule_backups, list_backups
from .paths import resource_dir

PUBLIC_DIR = resource_dir() / "public"

STATION_ID_RE = re.compile(r'^[A-Za-zА-Яа-яЁё0-9_\- ]{1,64}$')

app = FastAPI(title="КМ-трекинг")

# Инициализация и валидация БД — намеренно ВНЕ асинхронного startup-хука,
# прямо на уровне импорта модуля. Если конфиг наборов битый, init_db()
# вызовет sys.exit(1) синхронно, ДО того как uvicorn вообще запустит
# ASGI/lifespan — иначе SystemExit из хука startup оборачивается Starlette
# в пугающий async-трейсбек поверх и без того понятного сообщения об ошибке.
init_db()


def _check_station_id(station_id: str) -> str:
    station_id = (station_id or "").strip()
    if not STATION_ID_RE.match(station_id):
        raise HTTPException(
            status_code=400,
            detail="Некорректный ID станции: 1-64 символа, буквы/цифры/пробел/дефис/подчёркивание."
        )
    return station_id


class ScanRequest(BaseModel):
    code: str = ""


@app.on_event("startup")
def on_startup():
    schedule_backups(15)
    try:
        run_backup("startup")
    except Exception as err:
        print(f"[backup] ошибка при старте: {err}")


# ---------- служебное ----------
@app.get("/api/health")
def health():
    from datetime import datetime, timezone
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/templates")
def templates():
    rows = conn.execute("SELECT * FROM kit_templates ORDER BY id").fetchall()
    result = []
    for t in rows:
        items = conn.execute(
            "SELECT * FROM kit_template_items WHERE kit_template_id=?", (t["id"],)
        ).fetchall()
        d = dict(t)
        d["items"] = [dict(i) for i in items]
        result.append(d)
    return result


# ---------- приём сканов (рабочее место станции) ----------
@app.post("/api/stations/{station_id}/scan")
def scan(station_id: str, body: ScanRequest):
    station_id = _check_station_id(station_id)
    result = process_scan(station_id, body.code)
    result["state"] = get_station_state(station_id)
    return result


@app.get("/api/stations/{station_id}/state")
def state(station_id: str):
    station_id = _check_station_id(station_id)
    return get_station_state(station_id)


# ---------- контроль / супервайзер ----------
@app.get("/api/tree")
def tree(q: str = Query(default="")):
    return build_tree(q)


@app.get("/api/search")
def search(code: str = Query(default="")):
    return search_code(code)


@app.get("/api/stats")
def stats():
    return get_stats()


# ---------- отчёт для ТО ----------
@app.get("/api/report/preview")
def report_preview(limit: int = Query(default=200, ge=1, le=5000)):
    return {"rows": report_rows(limit), "total": report_count()}


@app.get("/api/export.csv")
def export():
    csv_text = export_csv()
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="km_export.csv"'},
    )


# ---------- бэкап ----------
@app.post("/api/backup")
def backup_now():
    try:
        path = run_backup("manual")
        return {"ok": True, "file": path.name}
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.get("/api/backups")
def backups():
    return list_backups()


# ---------- статика фронтенда (регистрируется ПОСЛЕДНЕЙ — иначе перекроет /api/*) ----------
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
