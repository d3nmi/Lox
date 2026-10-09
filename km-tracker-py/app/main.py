# ============================================================
# Локальный сервер учёта иерархии КМ — один склад, до 20 станций.
# Этап 3 плана реализации, порт на Python/FastAPI/SQLite.
# ============================================================
import logging
import re

from fastapi import FastAPI, HTTPException, Response, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import conn, init_db
from .validation import process_scan, get_station_state, select_kit, set_station_settings, ensure_box_schema
from .control import build_tree, search_code, get_stats
from .station_stats import get_station_stats
from .report import export_csv, report_rows, report_count, export_data_csv, export_data_rows, export_data_count
from .backup import run_backup, schedule_backups, list_backups
from .paths import resource_dir
from .errors import install_error_handlers

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("km")

PUBLIC_DIR = resource_dir() / "public"

STATION_ID_RE = re.compile(r'^[A-Za-zА-Яа-яЁё0-9_\- ]{1,64}$')
MAX_CODE_LEN = 300   # реальные КМ — до ~130 символов; всё длиннее — явный мусор/ошибка ввода

app = FastAPI(title="КМ-трекинг")
install_error_handlers(app)   # пользователь не увидит traceback/SQL/пути (см. errors.py)

# Инициализация и валидация БД — намеренно ВНЕ асинхронного startup-хука,
# прямо на уровне импорта модуля. Если конфиг наборов битый, init_db()
# вызовет sys.exit(1) синхронно, ДО того как uvicorn вообще запустит
# ASGI/lifespan — иначе SystemExit из хука startup оборачивается Starlette
# в пугающий async-трейсбек поверх и без того понятного сообщения об ошибке.
init_db()
ensure_box_schema()  # настройки станции (режим «Код короба») и kits.box_no — см. validation.py


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


class SelectKitRequest(BaseModel):
    kit_code: str = ""


class SettingsRequest(BaseModel):
    require_box: bool = False


@app.on_event("startup")
def on_startup():
    schedule_backups(15)
    try:
        run_backup("startup")
    except Exception:
        log.exception("Ошибка резервного копирования при старте")


# ---------- служебное ----------
@app.get("/api/health")
def health():
    from datetime import datetime, timezone
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/templates")
def templates():
    """Справочник наборов для выбора на станции. ready=false — в конфиге у набора
    остались позиции без GTIN, выбрать такой набор нельзя (missing — какие именно)."""
    rows = conn.execute("SELECT * FROM kit_templates ORDER BY id").fetchall()
    result = []
    for t in rows:
        items = conn.execute(
            "SELECT * FROM kit_template_items WHERE kit_template_id=? ORDER BY id", (t["id"],)
        ).fetchall()
        d = dict(t)
        d["items"] = [dict(i, marked=bool(i["marked"])) for i in items]
        d["missing"] = [i["item_name"] for i in items if not i["item_sku"]]
        d["ready"] = not d["missing"]
        result.append(d)
    return result


# ---------- приём сканов (рабочее место станции) ----------
@app.post("/api/stations/{station_id}/select-kit")
def choose_kit(station_id: str, body: SelectKitRequest):
    station_id = _check_station_id(station_id)
    result = select_kit(station_id, body.kit_code)
    result["state"] = get_station_state(station_id)
    return result


@app.post("/api/stations/{station_id}/settings")
def station_settings(station_id: str, body: SettingsRequest):
    station_id = _check_station_id(station_id)
    result = set_station_settings(station_id, body.require_box)
    result["state"] = get_station_state(station_id)
    return result


@app.post("/api/stations/{station_id}/scan")
def scan(station_id: str, body: ScanRequest):
    station_id = _check_station_id(station_id)
    if len(body.code) > MAX_CODE_LEN:
        # слишком длинный ввод в БД и журнал не пишем
        result = {'result': 'error', 'code_type': 'unknown',
                  'message': 'Слишком длинный код — это не КМ. Очистите поле и отсканируйте код ещё раз.'}
    else:
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


@app.get("/api/stats/stations")
def stats_stations():
    """Работа столов за сегодня: наборы, ошибки, скорость."""
    return get_station_stats()


# ---------- страница «Контроль» ----------
# Отдельная страница со своей ссылкой (печатается при старте сервера);
# в интерфейсе станций её нет.
@app.get("/control", include_in_schema=False)
def control_page():
    return FileResponse(str(PUBLIC_DIR / "control.html"))


# ---------- отчёт для ТО ----------
# Отдельная страница со своей ссылкой (адрес печатается при старте сервера);
# в основном интерфейсе станций её нет.
@app.get("/report", include_in_schema=False)
def report_page():
    return FileResponse(str(PUBLIC_DIR / "report.html"))


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


# ---------- отдельная выгрузка: набор / вложение / короб ----------
@app.get("/api/export-data/preview")
def export_data_preview(limit: int = Query(default=200, ge=1, le=5000)):
    return {"rows": export_data_rows(limit), "total": export_data_count()}


@app.get("/api/export-data.csv")
def export_data_export():
    csv_text = export_data_csv()
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="km_export_data.csv"'},
    )


# ---------- бэкап ----------
@app.post("/api/backup")
def backup_now():
    try:
        path = run_backup("manual")
        return {"ok": True, "file": path.name}
    except Exception:
        log.exception("Ошибка ручного резервного копирования")
        # текст исключения (может содержать пути) наружу не отдаём
        raise HTTPException(status_code=500, detail="Не удалось создать резервную копию. Подробности — в окне сервера.")


@app.get("/api/backups")
def backups():
    return list_backups()


# ---------- статика фронтенда (регистрируется ПОСЛЕДНЕЙ — иначе перекроет /api/*) ----------
app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")
