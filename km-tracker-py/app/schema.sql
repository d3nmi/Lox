-- ============================================================
-- Схема БД: иерархия КМ (товар -> комплект -> короб -> паллета)
-- Один склад, до 20 одновременных станций.
--
-- Ключевое отличие от многоскладской версии: "открытый набор" — это
-- состояние КОНКРЕТНОЙ СТАНЦИИ (station_id), не склада. Каждая станция
-- ведёт свой набор независимо от остальных 19. Короб и паллета — общий
-- пул на весь склад: закрытие короба забирает все закрытые-но-неупакованные
-- наборы от ЛЮБЫХ станций, а не только от той, что скана��ирует агрегат короба.
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Справочник шаблонов комплектов (какие товары входят в комплект)
CREATE TABLE IF NOT EXISTS kit_templates (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kit_sku     TEXT NOT NULL UNIQUE,   -- GTIN комплекта (агрегата)
  kit_name    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kit_template_items (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  kit_template_id  INTEGER NOT NULL REFERENCES kit_templates(id) ON DELETE CASCADE,
  item_sku         TEXT NOT NULL,     -- GTIN товара
  item_name        TEXT NOT NULL,
  qty_required     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_kti_template ON kit_template_items(kit_template_id);
CREATE INDEX IF NOT EXISTS idx_kti_sku ON kit_template_items(item_sku);

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
  kit_sku        TEXT NOT NULL,
  kit_name       TEXT NOT NULL,
  km_agg_code    TEXT UNIQUE,          -- заполняется при закрытии комплекта
  station_id     TEXT NOT NULL,
  box_id         INTEGER REFERENCES boxes(id),
  status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  items_count    INTEGER NOT NULL DEFAULT 0,
  items_required INTEGER NOT NULL DEFAULT 0,
  scan_order     INTEGER,
  opened_at      TEXT,
  closed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_kits_box ON kits(box_id);
CREATE INDEX IF NOT EXISTS idx_kits_station_status ON kits(station_id, status);

-- Товары (единичные КМ)
CREATE TABLE IF NOT EXISTS items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  km_code      TEXT NOT NULL UNIQUE,
  item_sku     TEXT NOT NULL,
  item_name    TEXT NOT NULL,
  kit_id       INTEGER NOT NULL REFERENCES kits(id) ON DELETE CASCADE,
  station_id   TEXT NOT NULL,
  scanned_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_kit ON items(kit_id);
CREATE INDEX IF NOT EXISTS idx_items_code ON items(km_code);

-- Отдельная выгрузка: набор / вложение / короб (Этап 4.3+), не зависит
-- от items/kits/boxes при чтении. По строке на каждое вложение появляется
-- в момент закрытия набора; km_box_code сперва NULL — наполняемость короба
-- наборами становится известна позже, при закрытии короба
-- (см. validation.py: _handle_item_or_kit_agg заполняет строку,
-- _close_box дозаполняет km_box_code).
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
-- Составной индекс покрывает и фильтр по станции, и сортировку ленты по id —
-- без него SQLite делает TEMP B-TREE на всю историю станции при каждом опросе.
CREATE INDEX IF NOT EXISTS idx_scanlog_station_id ON scan_log(station_id, id);
CREATE INDEX IF NOT EXISTS idx_scanlog_time ON scan_log(created_at);

-- Общий счётчик порядка сканирования
CREATE TABLE IF NOT EXISTS scan_seq (
  id   INTEGER PRIMARY KEY CHECK (id = 1),
  val  INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO scan_seq (id, val) VALUES (1, 0);

-- Быстрая проверка глобальной уникальности ЛЮБОГО отсканированного кода
CREATE TABLE IF NOT EXISTS used_codes (
  code         TEXT PRIMARY KEY,
  code_type    TEXT NOT NULL,
  station_id   TEXT NOT NULL,
  used_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Гарантия на уровне БД: не более одного ОТКРЫТОГО набора НА СТАНЦИЮ
-- одновременно (не на склад — иначе 20 станций мешали бы друг другу).
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_kit_per_station ON kits(station_id) WHERE status = 'open';
