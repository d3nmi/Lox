# ============================================================
# Статистика по столам (станциям) за сегодня: сколько наборов собрано,
# сколько ошибок, как быстро работает стол. Только ЧТЕНИЕ существующих
# таблиц (kits, items, scan_log) — схема БД не меняется.
# ============================================================
from datetime import datetime
from statistics import median

from .db import conn, read_lock

MIN_SPAN_SEC = 600   # меньше 10 минут работы — «наборов в час» не считаем (цифра была бы случайной)


def _parse(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def get_station_stats():
    with read_lock():
        scans = conn.execute(
            "SELECT station_id, COUNT(*) AS scans, "
            "SUM(CASE WHEN result='error' THEN 1 ELSE 0 END) AS errors, "
            "MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM scan_log WHERE date(created_at)=date('now','localtime') GROUP BY station_id"
        ).fetchall()
        kits = conn.execute(
            "SELECT station_id, opened_at, closed_at FROM kits "
            "WHERE status='closed' AND date(closed_at)=date('now','localtime')"
        ).fetchall()
        items = conn.execute(
            "SELECT station_id, COUNT(*) AS n FROM items "
            "WHERE date(scanned_at)=date('now','localtime') GROUP BY station_id"
        ).fetchall()
        today = conn.execute("SELECT date('now','localtime') AS d").fetchone()["d"]

    by = {}

    def row(st):
        return by.setdefault(st, {
            'station_id': st, 'kits': 0, 'items': 0, 'scans': 0, 'errors': 0, 'error_pct': 0,
            'first': None, 'last': None, 'kits_per_hour': None, 'median_kit_sec': None,
        })

    for r in scans:
        d = row(r["station_id"])
        d['scans'] = r["scans"]
        d['errors'] = r["errors"] or 0
        d['first'], d['last'] = r["first_at"], r["last_at"]
    for r in items:
        row(r["station_id"])['items'] = r["n"]

    durations = {}
    for r in kits:
        row(r["station_id"])['kits'] += 1
        a, b = _parse(r["opened_at"]), _parse(r["closed_at"])
        if a and b and b >= a:
            durations.setdefault(r["station_id"], []).append((b - a).total_seconds())

    for st, d in by.items():
        if d['scans']:
            d['error_pct'] = round(100.0 * d['errors'] / d['scans'], 1)
        if durations.get(st):
            d['median_kit_sec'] = int(median(durations[st]))
        a, b = _parse(d['first']), _parse(d['last'])
        if a and b and d['kits']:
            span = (b - a).total_seconds()
            if span >= MIN_SPAN_SEC:
                d['kits_per_hour'] = round(d['kits'] * 3600.0 / span, 1)

    stations = sorted(by.values(), key=lambda d: (-d['kits'], d['station_id']))
    total = {
        'kits': sum(d['kits'] for d in stations),
        'items': sum(d['items'] for d in stations),
        'scans': sum(d['scans'] for d in stations),
        'errors': sum(d['errors'] for d in stations),
    }
    return {'date': today, 'stations': stations, 'total': total}
