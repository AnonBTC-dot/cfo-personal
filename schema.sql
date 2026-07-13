-- CFO Personal — schema.sql
-- Ejecutar una vez: sqlite3 ~/cfo/cfo.db < ~/cfo/schema.sql

CREATE TABLE IF NOT EXISTS inversiones_personal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    activo TEXT NOT NULL,
    tipo TEXT NOT NULL,
    cantidad REAL,
    precio_unitario REAL,
    monto_usd REAL NOT NULL,
    monto_cop REAL,
    moneda TEXT DEFAULT 'USD',
    notas TEXT,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inversiones_family (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    activo TEXT NOT NULL,
    tipo TEXT NOT NULL,
    cantidad REAL,
    precio_unitario REAL,
    monto_usd REAL NOT NULL,
    monto_cop REAL,
    moneda TEXT DEFAULT 'USD',
    notas TEXT,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inversiones_papas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    activo TEXT NOT NULL,
    tipo TEXT NOT NULL,
    cantidad REAL,
    precio_unitario REAL,
    monto_usd REAL NOT NULL,
    monto_cop REAL,
    moneda TEXT DEFAULT 'USD',
    notas TEXT,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categorias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL UNIQUE,
    limite_mensual REAL,
    tipo TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transacciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    categoria_id INTEGER REFERENCES categorias(id),
    monto REAL NOT NULL,
    descripcion TEXT,
    tipo TEXT NOT NULL,
    moneda TEXT DEFAULT 'USD',
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    monto_objetivo REAL NOT NULL,
    fecha_objetivo TEXT,
    monto_actual REAL DEFAULT 0,
    activa INTEGER DEFAULT 1,
    notas TEXT,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO categorias (nombre, limite_mensual, tipo) VALUES
    ('Salario STG', NULL, 'ingreso'),
    ('Vivienda', NULL, 'gasto'),
    ('Alimentación', NULL, 'gasto'),
    ('Transporte', NULL, 'gasto'),
    ('Salud', NULL, 'gasto'),
    ('Entretenimiento', NULL, 'gasto'),
    ('Inversiones', NULL, 'ahorro'),
    ('Ahorro emergencia', NULL, 'ahorro');
