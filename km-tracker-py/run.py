"""
Точка входа для запуска сервера — как в разработке (`python run.py`),
так и из собранного PyInstaller .exe (см. build_exe.md).

Печатает адреса, по которым сервер доступен в локальной сети — это
именно то, что нужно вписать в браузере на каждой из 20 станций.
"""
import os
import socket
import sys

import uvicorn

from app.main import app  # прямой импорт, а не строка "app.main:app" —
# со строковой формой PyInstaller может не найти модуль при сборке .exe,
# так как не отслеживает импорты, указанные текстом

PORT = int(os.environ.get("PORT", "8000"))


def local_ip():
    """Лучшее предположение о LAN-адресе этого ПК (не 127.0.0.1) —
    чтобы сразу показать оператору, что вводить на станциях."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    ip = local_ip()
    print("=" * 60)
    print("КМ-сервер запускается...")
    print(f"  На этом компьютере:  http://localhost:{PORT}")
    if ip:
        print(f"  С других станций в сети:  http://{ip}:{PORT}")
        print(f"Для загрузки отчёта ТО:  http://{ip}:{PORT}/report")
        print(f"Контроль и статистика:   http://{ip}:{PORT}/control")
    else:
        print("  Не удалось определить IP в локальной сети — узнайте его")
        print("  командой `ipconfig` (Windows) и подставьте вместо localhost.")
        print(f"Для загрузки отчёта ТО:  http://localhost:{PORT}/report")
        print(f"Контроль и статистика:   http://localhost:{PORT}/control")
    print("  Не закрывайте это окно, пока сервер должен работать.")
    print("=" * 60)

    # host 0.0.0.0 — сервер слушает на всех сетевых интерфейсах,
    # иначе другие станции в локальной сети не смогут подключиться.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
