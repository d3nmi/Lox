# ============================================================
# Безопасная обработка ошибок сервера.
# Пользователь видит только короткое понятное сообщение. Подробности
# (traceback, SQL, пути) пишутся ТОЛЬКО в консоль сервера — для разработчика.
# ============================================================
import logging
import sqlite3

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

log = logging.getLogger("km")

MSG_SERVER = "Ошибка на сервере. Повторите действие. Если ошибка повторяется — позовите администратора."
MSG_DB = "Временная ошибка базы данных. Повторите действие через несколько секунд."
MSG_BAD_REQUEST = "Некорректный запрос. Обновите страницу и повторите."


def install_error_handlers(app):
    @app.exception_handler(sqlite3.Error)
    async def _db_error(request: Request, exc: sqlite3.Error):
        log.exception("Ошибка БД: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=503, content={"detail": MSG_DB})

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError):
        log.warning("Некорректный запрос: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=422, content={"detail": MSG_BAD_REQUEST})

    @app.exception_handler(Exception)
    async def _any_error(request: Request, exc: Exception):
        log.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": MSG_SERVER})
