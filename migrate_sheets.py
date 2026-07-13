"""
migrate_sheets.py — Migración específica para los Google Sheets de Leonardo
Formato detectado: 2026-06-26

Archivos esperados en ~/cfo/exports/:
  Inversiones.csv       — Leonardo (cols 1-14) + Papás (cols 16-28) en la misma hoja
  inversiones_family.csv — BTC family, header en fila 4
  Dinero.csv            — Budget mensual en COP → importa categorías + límites

Correr: python3 ~/cfo/migrate_sheets.py
"""

import sqlite3
import pandas as pd
from pathlib import Path

DB_PATH = Path.home() / "cfo/cfo.db"
EXPORTS = Path.home() / "cfo/exports"


def get_conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def clean_usd(val) -> float:
    """'$5,209' o '5209.0' → 5209.0"""
    if pd.isna(val):
        return 0.0
    s = str(val).replace("$", "").replace(",", "").replace(" ", "").strip()
    if not s or s in ("-", "0", ""):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def make_date(dia, mes, ano) -> str | None:
    try:
        d = int(float(str(dia)))
        m = int(float(str(mes)))
        a = int(float(str(ano)))
        if a < 1990 or a > 2100 or m < 1 or m > 12 or d < 1 or d > 31:
            return None
        return f"{a:04d}-{m:02d}-{d:02d}"
    except Exception:
        return None


# ── 1. Inversiones.csv (Leonardo cols 1-14 + Papás cols 16-28) ──────────────

def import_inversiones():
    # Acepta Inversiones.csv o inversiones.csv
    path = next((EXPORTS / n for n in ("Inversiones.csv", "inversiones.csv") if (EXPORTS / n).exists()), None)
    if not path:
        print("⚠️  Inversiones.csv no encontrado en exports/")
        return

    df = pd.read_csv(path, header=None, dtype=str)
    db = get_conn()
    personal_ok = papas_ok = 0

    # Datos empiezan en fila 6 (0-indexed)
    for i in range(6, len(df)):
        row = df.iloc[i]

        # ── Leonardo ────────────────────────────────────────────────────────
        tipo = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        if tipo in ("Compra", "Venta"):
            fecha = make_date(row.iloc[3], row.iloc[4], row.iloc[5])
            monto = clean_usd(row.iloc[7])       # Cantidad (USD)
            cant_btc = clean_usd(row.iloc[8])    # Cantidad (BTC)
            precio = clean_usd(row.iloc[9])      # Valor de compra (precio BTC ese día)
            lugar = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ""

            if fecha and monto > 0:
                # Ventas como negativo → SUM = net invertido real
                monto_neto = -monto if tipo == "Venta" else monto
                db.execute("""
                    INSERT INTO inversiones_personal
                        (fecha, activo, tipo, cantidad, precio_unitario, monto_usd, notas)
                    VALUES (?, 'BTC', ?, ?, ?, ?, ?)
                """, (fecha, tipo.lower(), cant_btc or None, precio or None, monto_neto,
                      f"Lugar: {lugar}" if lugar else ""))
                personal_ok += 1

        # ── Papás (sin columna Lugar, empiezan en col 16) ───────────────────
        tipo_p = str(row.iloc[16]).strip() if len(row) > 16 and pd.notna(row.iloc[16]) else ""
        if tipo_p in ("Compra", "Venta"):
            fecha_p = make_date(row.iloc[17], row.iloc[18], row.iloc[19])
            monto_p  = clean_usd(row.iloc[21])   # Cantidad (USD)
            cant_p   = clean_usd(row.iloc[22])   # Cantidad (BTC)
            precio_p = clean_usd(row.iloc[23])   # Valor de compra

            if fecha_p and monto_p > 0:
                monto_p_neto = -monto_p if tipo_p == "Venta" else monto_p
                db.execute("""
                    INSERT INTO inversiones_papas
                        (fecha, activo, tipo, cantidad, precio_unitario, monto_usd)
                    VALUES (?, 'BTC', ?, ?, ?, ?)
                """, (fecha_p, tipo_p.lower(), cant_p or None, precio_p or None, monto_p_neto))
                papas_ok += 1

    db.commit()
    db.close()
    print(f"✅ inversiones_personal : {personal_ok} filas (Compras + Ventas)")
    print(f"✅ inversiones_papas    : {papas_ok} filas (Compras + Ventas)")


# ── 2. inversiones_family.csv ────────────────────────────────────────────────

def import_family():
    path = EXPORTS / "inversiones_family.csv"
    if not path.exists():
        print("⚠️  inversiones_family.csv no encontrado en exports/")
        return

    # Header en fila 4 (0-indexed)
    df = pd.read_csv(path, header=4, dtype=str)
    db = get_conn()
    ok = 0

    for _, row in df.iterrows():
        tipo = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        if tipo not in ("Compra", "Venta"):
            continue

        fecha    = make_date(row.iloc[2], row.iloc[3], row.iloc[4])
        monto    = clean_usd(row.iloc[6])   # Cantidad (USD)
        cant_btc = clean_usd(row.iloc[7])   # Cantidad (BTC)
        precio   = clean_usd(row.iloc[8])   # Valor de compra
        lugar    = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""

        if fecha and monto > 0:
            monto_neto = -monto if tipo == "Venta" else monto
            db.execute("""
                INSERT INTO inversiones_family
                    (fecha, activo, tipo, cantidad, precio_unitario, monto_usd, notas)
                VALUES (?, 'BTC', ?, ?, ?, ?, ?)
            """, (fecha, tipo.lower(), cant_btc or None, precio or None, monto_neto,
                  f"Lugar: {lugar}" if lugar else ""))
            ok += 1

    db.commit()
    db.close()
    print(f"✅ inversiones_family   : {ok} filas (Compras + Ventas)")


# ── 3. Dinero.csv → categorías con límites mensuales (en COP) ───────────────

def import_dinero():
    path = next((EXPORTS / n for n in ("Dinero.csv", "dinero.csv") if (EXPORTS / n).exists()), None)
    if not path:
        print("⚠️  Dinero.csv no encontrado en exports/")
        return

    df = pd.read_csv(path, header=None, dtype=str)
    db = get_conn()
    ok = 0

    tipo_map = {"esenciales": "gasto", "otros": "gasto", "ahorro": "ahorro"}
    skip = {"", "Gastos", "TOTAL", "Dinero", "USD", "COP", "Monto", "Cantidad"}

    for i in range(len(df)):
        row = df.iloc[i]
        cat = str(row.iloc[2]).strip() if pd.notna(row.iloc[2]) else ""
        if cat in skip or cat.startswith("}"):
            continue

        tipo_raw = str(row.iloc[7]).strip().lower() if len(row) > 7 and pd.notna(row.iloc[7]) else ""
        tipo = tipo_map.get(tipo_raw, "gasto")

        budget_raw = str(row.iloc[3]).strip() if pd.notna(row.iloc[3]) else "0"
        try:
            budget = float(budget_raw.replace(",", "").replace("$", "").strip())
        except ValueError:
            budget = None

        existing = db.execute("SELECT id FROM categorias WHERE nombre=?", (cat,)).fetchone()
        if existing:
            if budget:
                db.execute("UPDATE categorias SET limite_mensual=?, tipo=? WHERE nombre=?",
                           (budget, tipo, cat))
        else:
            db.execute("INSERT INTO categorias (nombre, limite_mensual, tipo) VALUES (?,?,?)",
                       (cat, budget, tipo))
            ok += 1

    db.commit()
    db.close()
    print(f"✅ dinero/categorías    : {ok} categorías importadas")
    print("   ℹ️  Límites en COP — transacciones se agregan via Telegram o dashboard")


# ────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("🚀 Migrando Google Sheets → SQLite\n")
    import_inversiones()
    import_family()
    import_dinero()

    # Resumen rápido
    db = get_conn()
    p  = db.execute("SELECT COUNT(*), COALESCE(SUM(monto_usd),0) FROM inversiones_personal").fetchone()
    f  = db.execute("SELECT COUNT(*), COALESCE(SUM(monto_usd),0) FROM inversiones_family").fetchone()
    pa = db.execute("SELECT COUNT(*), COALESCE(SUM(monto_usd),0) FROM inversiones_papas").fetchone()
    db.close()

    print(f"""
╔══════════════════════════════════════════════╗
║  Resumen de migración                        ║
╠══════════════════════════════════════════════╣
║  Personal : {p[0]:>3} filas · ${p[1]:>12,.2f} USD   ║
║  Family   : {f[0]:>3} filas · ${f[1]:>12,.2f} USD   ║
║  Papás    : {pa[0]:>3} filas · ${pa[1]:>12,.2f} USD   ║
╚══════════════════════════════════════════════╝

Dashboard: http://localhost:3100
""")
