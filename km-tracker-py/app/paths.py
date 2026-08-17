# ============================================================
# Разрешение путей: единое место, учитывающее разницу между обычным
# запуском (`python run.py`) и собранным PyInstaller .exe.
#
# Два РАЗНЫХ понятия пути, которые нельзя путать при сборке в .exe:
#
#   resource_dir() — папка с файлами, УПАКОВАННЫМИ ВНУТРЬ exe (schema.sql,
#     дефолтный kit-templates.json, public/index.html и app.js). При
#     запуске из собранного .exe PyInstaller распаковывает их во ВРЕМЕННУЮ
#     папку (sys._MEIPASS), которая создаётся заново при каждом старте и
#     удаляется при выходе. Использовать только для ЧТЕНИЯ.
#
#   app_dir() — папка, где живут ИЗМЕНЯЕМЫЕ данные (data/km.db,
#     data/backups/). Должна быть РЯДОМ С EXE-файлом, а не во временной
#     распаковке — иначе вся БД будет молча исчезать при каждом перезапуске
#     сервера. Совпадает с resource_dir() в режиме разработки (когда
#     программа не заморожена PyInstaller-ом).
# ============================================================
import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_dir() -> Path:
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent
