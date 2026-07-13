-- CFO Personal — schema PostgreSQL (Supabase)

CREATE TABLE IF NOT EXISTS inversiones_personal (
    id SERIAL PRIMARY KEY,
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
    id SERIAL PRIMARY KEY,
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
    id SERIAL PRIMARY KEY,
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
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL UNIQUE,
    limite_mensual REAL,
    tipo TEXT NOT NULL,
    moneda TEXT DEFAULT 'USD'
);

CREATE TABLE IF NOT EXISTS transacciones (
    id SERIAL PRIMARY KEY,
    fecha TEXT NOT NULL,
    categoria_id INTEGER REFERENCES categorias(id),
    monto REAL NOT NULL,
    descripcion TEXT,
    tipo TEXT NOT NULL,
    moneda TEXT DEFAULT 'USD',
    afecta_cash INTEGER DEFAULT 1,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS metas (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    monto_objetivo REAL NOT NULL,
    fecha_objetivo TEXT,
    monto_actual REAL DEFAULT 0,
    activa INTEGER DEFAULT 1,
    tipo TEXT DEFAULT 'cash',
    btc_desde TEXT,
    notas TEXT,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meta_depositos (
    id SERIAL PRIMARY KEY,
    meta_id INTEGER NOT NULL,
    monto REAL NOT NULL,
    moneda TEXT DEFAULT 'USD',
    cuenta TEXT,
    fecha TEXT,
    notas TEXT,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ahorros (
    id SERIAL PRIMARY KEY,
    descripcion TEXT NOT NULL,
    monto REAL NOT NULL,
    moneda TEXT DEFAULT 'USD',
    fecha_actualizacion TEXT,
    notas TEXT,
    cuenta TEXT
);

CREATE TABLE IF NOT EXISTS deudas (
    id SERIAL PRIMARY KEY,
    descripcion TEXT NOT NULL,
    monto REAL NOT NULL,
    moneda TEXT DEFAULT 'USD',
    notas TEXT
);

CREATE TABLE IF NOT EXISTS config (
    clave TEXT PRIMARY KEY,
    valor TEXT
);

CREATE TABLE IF NOT EXISTS precios_mercado (
    activo TEXT PRIMARY KEY,
    precio_usd REAL,
    actualizado_en TEXT
);
