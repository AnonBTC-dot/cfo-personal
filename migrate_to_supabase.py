"""
Migra datos de SQLite local a Supabase (PostgreSQL).
Uso: DATABASE_URL=<supabase_url> python3 migrate_to_supabase.py
"""
import sqlite3
import os
import sys
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Instala psycopg2: pip install psycopg2-binary")
    sys.exit(1)

SQLITE_DB = str(Path.home() / "cfo/cfo.db")
PG_URL = os.environ.get("DATABASE_URL")

if not PG_URL:
    print("ERROR: Falta DATABASE_URL")
    print("Uso: DATABASE_URL='postgresql://...' python3 migrate_to_supabase.py")
    sys.exit(1)

sqlite = sqlite3.connect(SQLITE_DB)
sqlite.row_factory = sqlite3.Row
pg = psycopg2.connect(PG_URL)
pg.autocommit = False
cur = pg.cursor()

TABLES = [
    "categorias",
    "transacciones",
    "metas",
    "meta_depositos",
    "ahorros",
    "deudas",
    "config",
    "precios_mercado",
    "inversiones_personal",
    "inversiones_family",
    "inversiones_papas",
]

def migrate_table(name):
    rows = sqlite.execute(f"SELECT * FROM {name}").fetchall()
    if not rows:
        print(f"  {name}: vacía, skip")
        return
    cols = list(rows[0].keys())
    placeholders = ",".join(["%s"] * len(cols))
    col_names = ",".join(cols)
    sql = f"INSERT INTO {name} ({col_names}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    data = [tuple(r[c] for c in cols) for r in rows]
    psycopg2.extras.execute_batch(cur, sql, data)
    print(f"  {name}: {len(rows)} filas migradas")

print("Creando schema en Supabase...")
with open("schema_postgres.sql") as f:
    cur.execute(f.read())

print("Migrando datos...")
for table in TABLES:
    try:
        migrate_table(table)
    except Exception as e:
        print(f"  ERROR en {table}: {e}")
        pg.rollback()
        sys.exit(1)

# Resetear secuencias SERIAL para que no haya conflictos en inserts futuros
serial_tables = [t for t in TABLES if t != "config" and t != "precios_mercado"]
for t in serial_tables:
    cur.execute(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE(MAX(id), 1)) FROM {t}")

pg.commit()
print("\nMigración completada.")
sqlite.close()
pg.close()
