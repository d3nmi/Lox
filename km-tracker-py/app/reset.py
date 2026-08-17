# ============================================================
# Очистка БД для тестового прогона (Этап 3.1).
# Запуск: python -m app.reset
# Справочник наборов сохраняется, удаляются только оперативные данные.
# ============================================================
from .db import conn, transaction, init_db


def main():
    init_db()
    answer = input(
        "Это удалит ВСЕ отсканированные данные (товары/комплекты/короба/паллеты/журнал) в текущей БД.\n"
        "Справочник наборов сохранится. Продолжить? (yes/no): "
    )
    if answer.strip().lower() != "yes":
        print("Отменено.")
        return

    with transaction():
        conn.execute("DELETE FROM items")
        conn.execute("DELETE FROM kits")
        conn.execute("DELETE FROM boxes")
        conn.execute("DELETE FROM pallets")
        conn.execute("DELETE FROM used_codes")
        conn.execute("DELETE FROM scan_log")
        conn.execute("UPDATE scan_seq SET val = 0 WHERE id = 1")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('items','kits','boxes','pallets','scan_log')")
    print("Готово: оперативные данные очищены.")


if __name__ == "__main__":
    main()
