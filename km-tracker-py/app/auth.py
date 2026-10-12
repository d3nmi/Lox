# ============================================================
# Пароль для настроек станции (галочка «Код короба»).
# Пароль берётся из файла pass.txt рядом с программой (рядом с km-server.exe /
# в корне проекта). Файл читается при КАЖДОЙ проверке — после смены пароля
# перезапускать сервер не нужно. Пароль в JavaScript и в базе данных не хранится.
#
# Формат pass.txt: строки, начинающиеся с «#», — комментарии; первая остальная
# непустая строка — пароль. Если файла нет, он создаётся с паролем по умолчанию.
# ============================================================
import hmac
import os
import time
from pathlib import Path

from .paths import app_dir

PASS_FILE = Path(os.environ.get("KM_PASS_FILE") or (app_dir() / "pass.txt"))
DEFAULT_PASSWORD = "1234"

_TEMPLATE = (
    "# Пароль для настроек станции (включение/выключение «Код короба»).\n"
    "# Строки, начинающиеся с #, — комментарии. Пароль — первая непустая строка ниже.\n"
    "# После смены пароля перезапускать сервер не нужно.\n"
    f"{DEFAULT_PASSWORD}\n"
)

MSG_WRONG = "Неверный пароль. Настройка не изменена."
MSG_NO_FILE = "Не найден или пуст файл pass.txt с паролем. Обратитесь к администратору."


def ensure_pass_file():
    """Создаёт pass.txt с паролем по умолчанию, если файла ещё нет."""
    if PASS_FILE.exists():
        return
    try:
        PASS_FILE.write_text(_TEMPLATE, encoding="utf-8")
        print(f"[КМ-сервер] Создан файл с паролем: {PASS_FILE}")
        print(f"            Пароль по умолчанию: {DEFAULT_PASSWORD} — замените его в этом файле.")
    except OSError as err:
        print(f"[КМ-сервер] Не удалось создать файл пароля {PASS_FILE}: {err}")


def _read_password():
    try:
        text = PASS_FILE.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def check_password(candidate):
    """-> (верно?, сообщение об ошибке)"""
    real = _read_password()
    if not real:
        return False, MSG_NO_FILE
    ok = hmac.compare_digest((candidate or "").strip().encode("utf-8"), real.encode("utf-8"))
    if not ok:
        time.sleep(1)   # небольшая задержка — перебор паролей становится бессмысленным
        return False, MSG_WRONG
    return True, ""
