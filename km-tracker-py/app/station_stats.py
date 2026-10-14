# ============================================================
# Статистика по столам (станциям) за выбранный день и по часам.
# Только ЧТЕНИЕ существующих таблиц (kits, items, scan_log) — схема БД не меняется.
#
# «Время работы» стола = сумма пауз между соседними сканами, которые не длиннее
# IDLE_GAP_SEC (5 минут). Перерывы, обед, простои в расчёт не входят — поэтому
# «наборов в час» и «товаров в час» показывают РЕАЛЬНУЮ скорость работы, а не
# среднее за смену с перерывами.
# ============================================================
import re
from datetime import datetime
from statistics import median

from .db import conn, read_lock

IDLE_GAP_SEC = 300     # пауза длиннее 5 минут — стол не работал
MIN_ACTIVE_SEC = 300   # меньше 5 минут работы — скорость не считаем (цифра была бы случайной)
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _parse(ts):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def valid_date(day):
    if not day or not _DATE_RE.match(day):
        return False
    try:
        datetime.strptime(day, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _rate(count, active_sec):
    if not count or active_sec < MIN_ACTIVE_SEC:
        return None
    return round(count * 3600.0 / active_sec, 1)


def get_station_stats(day=None):
    with read_lock():
        today = conn.execute("SELECT date('now','localtime') AS d").fetchone()["d"]
        day = day or today
        scans = conn.execute(
            "SELECT station_id, created_at, result FROM scan_log WHERE date(created_at)=? ORDER BY station_id, created_at, id",
            (day,)).fetchall()
        kits = conn.execute(
            "SELECT station_id, opened_at, closed_at FROM kits WHERE status='closed' AND date(closed_at)=?",
            (day,)).fetchall()
        items = conn.execute(
            "SELECT station_id, COUNT(*) AS n, SUM(CASE WHEN marked=1 THEN 1 ELSE 0 END) AS km "
            "FROM items WHERE date(scanned_at)=? GROUP BY station_id", (day,)).fetchall()
        kits_by_hour = conn.execute(
            "SELECT CAST(strftime('%H', closed_at) AS INTEGER) AS h, COUNT(*) AS n FROM kits "
            "WHERE status='closed' AND date(closed_at)=? GROUP BY h", (day,)).fetchall()
        items_by_hour = conn.execute(
            "SELECT CAST(strftime('%H', scanned_at) AS INTEGER) AS h, COUNT(*) AS n FROM items "
            "WHERE date(scanned_at)=? GROUP BY h", (day,)).fetchall()

    by = {}

    def row(st):
        return by.setdefault(st, {
            'station_id': st, 'kits': 0, 'items': 0, 'items_km': 0, 'scans': 0, 'errors': 0, 'error_pct': 0,
            'first': None, 'last': None, 'active_sec': 0,
            'kits_per_hour': None, 'items_per_hour': None, 'median_kit_sec': None,
        })

    prev_station, prev_time = None, None
    for r in scans:
        d = row(r["station_id"])
        d['scans'] += 1
        if r["result"] == 'error':
            d['errors'] += 1
        d['first'] = d['first'] or r["created_at"]
        d['last'] = r["created_at"]
        t = _parse(r["created_at"])
        if t and prev_station == r["station_id"] and prev_time:
            gap = (t - prev_time).total_seconds()
            if 0 <= gap <= IDLE_GAP_SEC:
                d['active_sec'] += int(gap)
        prev_station, prev_time = r["station_id"], t

    for r in items:
        d = row(r["station_id"])
        d['items'], d['items_km'] = r["n"], r["km"] or 0

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
        d['kits_per_hour'] = _rate(d['kits'], d['active_sec'])
        d['items_per_hour'] = _rate(d['items'], d['active_sec'])

    stations = sorted(by.values(), key=lambda d: (-d['kits'], -d['items'], d['station_id']))
    total = {
        'stations': len(stations),
        'kits': sum(d['kits'] for d in stations),
        'items': sum(d['items'] for d in stations),
        'scans': sum(d['scans'] for d in stations),
        'errors': sum(d['errors'] for d in stations),
        'active_sec': sum(d['active_sec'] for d in stations),
    }
    total['error_pct'] = round(100.0 * total['errors'] / total['scans'], 1) if total['scans'] else 0
    # средняя скорость ОДНОГО стола за час реальной работы (все столы вместе / суммарное время работы)
    total['kits_per_hour'] = _rate(total['kits'], total['active_sec'])
    total['items_per_hour'] = _rate(total['items'], total['active_sec'])

    kh = {r["h"]: r["n"] for r in kits_by_hour if r["h"] is not None}
    ih = {r["h"]: r["n"] for r in items_by_hour if r["h"] is not None}
    hours = sorted(set(kh) | set(ih))
    hourly = [{'hour': h, 'kits': kh.get(h, 0), 'items': ih.get(h, 0)} for h in range(hours[0], hours[-1] + 1)] if hours else []

    return {'date': day, 'is_today': day == today, 'stations': stations, 'total': total, 'hourly': hourly,
            'idle_gap_min': IDLE_GAP_SEC // 60}
