-- ============================================================
-- Схема БД: иерархия КМ (товар -> комплект -> короб -> паллета)
-- Один склад, до 20 одновременных станций.
--
-- "Открытый набор" — состояние КОНКРЕТНОЙ СТАНЦИИ (station_id). Каждая
-- станция ведёт свой набор независимо от остальных. Короб и паллета —
-- общий пул склада.
--
-- Справочник наборов (kit_templates / kit_template_items) НЕ хранит
-- ничего своего: при каждом старте сервера эти две таблицы пересоздаются
-- из app/config/kit-templates.json (см. db.py), на них нет внешних ключей
-- из рабочих таблиц — поэтому правка конфига не затрагивает отсканированные данные.
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Справочник наборов (пересоздаётся из конфига при старте)
CREATE TABLE IF NOT EXISTS kit_templates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kit_code    TEXT NOT NULL UNIQUE,   -- артикул набора (по нему выбирают набор)
  kit_sku     TEXT,                   -- GTIN набора (агрегата); может быть NULL
  kit_name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kit_template_items (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  kit_template_id  INTEGER NOT NULL REFERENCES kit_templates(id) ON DELETE CASCADE,
  item_code        TEXT NOT NULL,     -- артикул товара
  item_sku         TEXT,              -- GTIN товара; может быть NULL (набор тогда нельзя выбрать)
  item_name        TEXT NOT NULL,
  qty_required     INTEGER NOT NULL DEFAULT 1,
  marked           INTEGER NOT NULL DEFAULT 1  -- 1 = товар с КМ, 0 = упаковка без КМ
);
CREATE INDEX IF NOT EXISTS idx_kti_template ON kit_template_items(kit_template_id);
CREATE INDEX IF NOT EXISTS idx_kti_sku ON kit_template_items(item_sku);

-- Какой набор выбран на станции (запоминается между перезагрузками страницы)
CREATE TABLE IF NOT EXISTS station_selection (
  station_id  TEXT PRIMARY KEY,
  kit_code    TEXT NOT NULL,
  updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Паллеты — общий пул склада, без привязки к станции
CREATE TABLE IF NOT EXISTS pallets (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  pallet_code  TEXT NOT NULL UNIQUE,
  station_id   TEXT NOT NULL,        -- какая станция ЗАКРЫЛА паллету (для трассировки)
  status       TEXT NOT NULL DEFAULT 'closed' CHECK (status IN ('open','closed')),
  boxes_count  INTEGER NOT NULL DEFAULT 0,
  scan_order   INTEGER,
  opened_at    TEXT,
  closed_at    TEXT
);

-- Короба — общий пул склада
CREATE TABLE IF NOT EXISTS boxes (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  km_box_code   TEXT NOT NULL UNIQUE,
  station_id    TEXT NOT NULL,        -- какая станция ЗАКРЫЛА короб (для трассировки)
  pallet_id     INTEGER REFERENCES pallets(id),
  status        TEXT NOT NULL DEFAULT 'closed' CHECK (status IN ('open','closed')),
  kits_count    INTEGER NOT NULL DEFAULT 0,
  scan_order    INTEGER,
  opened_at     TEXT,
  closed_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_boxes_pallet ON boxes(pallet_id);

-- Комплекты (наборы) — у каждого своя станция-владелец, пока набор открыт
CREATE TABLE IF NOT EXISTS kits (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kit_sku        TEXT NOT NULL,        -- GTIN набора ('' если в справочнике не задан)
  kit_name       TEXT NOT NULL,
  km_agg_code    TEXT UNIQUE,          -- заполняется при закрытии комплекта
  station_id     TEXT NOT NULL,
  box_id         INTEGER REFERENCES boxes(id),
  status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  items_count    INTEGER NOT NULL DEFAULT 0,
  items_required INTEGER NOT NULL DEFAULT 0,
  scan_order     INTEGER,
  opened_at      TEXT,
  closed_at      TEXT,
  kit_code       TEXT                  -- артикул набора из справочника (добавлено позже, см. db._migrate)
);
CREATE INDEX IF NOT EXISTS idx_kits_box ON kits(box_id);
CREATE INDEX IF NOT EXISTS idx_kits_station_status ON kits(station_id, status);

-- Товары (единичные КМ и упаковка без КМ)
CREATE TABLE IF NOT EXISTS items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  km_code      TEXT NOT NULL UNIQUE,   -- для упаковки без КМ: "<штрихкод>#<n>" (уникальность служебная)
  item_sku     TEXT NOT NULL,
  item_name    TEXT NOT NULL,
  kit_id       INTEGER NOT NULL REFERENCES kits(id) ON DELETE CASCADE,
  station_id   TEXT NOT NULL,
  scanned_at   TEXT NOT NULL,
  marked       INTEGER NOT NULL DEFAULT 1   -- 1 = КМ (идёт в выгрузку), 0 = без КМ
);
CREATE INDEX IF NOT EXISTS idx_items_kit ON items(kit_id);
CREATE INDEX IF NOT EXISTS idx_items_code ON items(km_code);

-- Отдельная выгрузка: набор / вложение / короб, не зависит от items/kits/boxes
-- при чтении. Строки появляются в момент закрытия набора (только товары с КМ);
-- km_box_code сперва NULL и дозаполняется при добавлении набора в короб.
CREATE TABLE IF NOT EXISTS export_data (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kit_id        INTEGER NOT NULL REFERENCES kits(id) ON DELETE CASCADE,
  km_agg_code   TEXT NOT NULL,
  km_code       TEXT NOT NULL,
  km_box_code   TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_export_data_kit ON export_data(kit_id);

-- Журнал всех сканов (успешных и ошибочных) — для разбора инцидентов
CREATE TABLE IF NOT EXISTS scan_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  station_id    TEXT NOT NULL,
  code          TEXT NOT NULL,
  code_type     TEXT,                 -- item / kit_agg / box_agg / pallet / unknown
  result        TEXT NOT NULL CHECK (result IN ('ok','error')),
  message       TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_scanlog_station_id ON scan_log(station_id, id);
CREATE INDEX IF NOT EXISTS idx_scanlog_time ON scan_log(created_at);

-- Общий счётчик порядка сканирования
CREATE TABLE IF NOT EXISTS scan_seq (
  id   INTEGER PRIMARY KEY CHECK (id = 1),
  val  INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO scan_seq (id, val) VALUES (1, 0);

-- Быстрая проверка глобальной уникальности отсканированных GS1-кодов
CREATE TABLE IF NOT EXISTS used_codes (
  code         TEXT PRIMARY KEY,
  code_type    TEXT NOT NULL,
  station_id   TEXT NOT NULL,
  used_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Не более одного ОТКРЫТОГО набора НА СТАНЦИЮ одновременно.
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_kit_per_station ON kits(station_id) WHERE status = 'open';
