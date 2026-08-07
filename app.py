"""CFO Personal v2 — Puerto 3100"""

from flask import Flask, jsonify, render_template_string, request
from flask_cors import CORS
import os
from pathlib import Path
from datetime import datetime, date
import math

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

    ES_POSTGRES = True

    def _conn():
        return psycopg2.connect(DATABASE_URL)

    def q(sql, p=()):
        sql = sql.replace("?", "%s")
        c = _conn()
        try:
            cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, p)
            return [dict(x) for x in cur.fetchall()]
        finally:
            c.close()  # SIEMPRE, aunque la consulta falle: si no, se agota el pool

    def ex(sql, p=()):
        sql = sql.replace("?", "%s")
        sql = sql.replace("OR IGNORE INTO", "INTO").replace("INSERT OR IGNORE", "INSERT")
        c = _conn()
        try:
            cur = c.cursor()
            cur.execute(sql, p)
            c.commit()
        finally:
            c.close()

    def _tiene_columna(tabla, col):
        r = q("""SELECT 1 FROM information_schema.columns
                 WHERE table_name=? AND column_name=?""", (tabla, col))
        return bool(r)

else:
    import sqlite3
    DB = os.environ.get("CFO_DB_PATH", str(Path.home() / "cfo/cfo.db"))

    ES_POSTGRES = False

    def q(sql, p=()):
        c = sqlite3.connect(DB)
        try:
            c.row_factory = sqlite3.Row
            return [dict(x) for x in c.execute(sql, p).fetchall()]
        finally:
            c.close()

    def ex(sql, p=()):
        c = sqlite3.connect(DB)
        try:
            c.execute(sql, p)
            c.commit()
        finally:
            c.close()

    def _tiene_columna(tabla, col):
        return any(r["name"] == col for r in q(f"PRAGMA table_info({tabla})"))

def _migrar():
    """
    Añade transacciones.cuenta_id si falta.
    Se comprueba primero: lanzar un ALTER que falla en cada arranque dejaba
    conexiones colgadas y acababa tumbando la app en Postgres.
    """
    try:
        if not _tiene_columna("transacciones", "cuenta_id"):
            ex("ALTER TABLE transacciones ADD COLUMN cuenta_id INTEGER")
            print("[migración] transacciones.cuenta_id creada")
    except Exception as e:
        print(f"[migración] no se pudo verificar/crear cuenta_id: {e}")

_migrar()


def cfg(k): r = q("SELECT valor FROM config WHERE clave=?", (k,)); return r[0]["valor"] if r else None


# ── APIs ──────────────────────────────────────────────────────────────────────

@app.route("/api/networth")
def networth():
    tasa = float(cfg("tasa_cop_usd") or 4050)
    tasa_pyg = float(cfg("tasa_pyg_usd") or 7300)
    precios = {x["activo"]: x["precio_usd"] for x in q("SELECT * FROM precios_mercado")}

    tablas = ["inversiones_personal", "inversiones_family", "inversiones_papas"]
    labels = ["personal", "family", "papas"]
    inv = {}
    for t, l in zip(tablas, labels):
        rows = q(f"SELECT activo, SUM(monto_usd) as cb, SUM(CASE WHEN monto_usd>0 THEN cantidad ELSE -COALESCE(cantidad,0) END) as qty FROM {t} GROUP BY activo")
        for r in rows:
            p = precios.get(r["activo"])
            val = r["qty"] * p if (p and r["qty"]) else None
            inv.setdefault(r["activo"], {"activo": r["activo"], "costo_base": 0, "qty": 0, "portfolios": []})
            inv[r["activo"]]["costo_base"] += r["cb"] or 0
            inv[r["activo"]]["qty"] += r["qty"] or 0
            inv[r["activo"]]["portfolios"].append(l)

    activos = list(inv.values())
    for a in activos:
        p = precios.get(a["activo"])
        a["precio_actual"] = p
        if p and a["qty"]:
            a["valor_actual"] = round(a["qty"] * p, 2)
            a["ganancia"] = round(a["valor_actual"] - a["costo_base"], 2)
            a["ganancia_pct"] = round(a["ganancia"] / a["costo_base"] * 100, 1) if a["costo_base"] else 0
        else:
            a["valor_actual"] = None; a["ganancia"] = None; a["ganancia_pct"] = None

    cash_usd = q("SELECT COALESCE(SUM(monto),0) as t FROM ahorros WHERE moneda='USD'")[0]["t"]
    cash_cop = q("SELECT COALESCE(SUM(monto),0) as t FROM ahorros WHERE moneda='COP'")[0]["t"]
    deudas = q("SELECT COALESCE(SUM(monto),0) as t FROM deudas WHERE moneda='USD'")[0]["t"]

    # Adjust cash with current month gasto/ingreso only — identical logic to budget/cash
    mes = datetime.now().strftime("%Y-%m")
    flujo = q("""SELECT c.moneda, t.tipo, COALESCE(SUM(t.monto),0) as total
        FROM transacciones t JOIN categorias c ON t.categoria_id=c.id
        WHERE t.fecha LIKE ? AND t.tipo IN ('gasto','ingreso') AND COALESCE(t.afecta_cash,1)=1
        GROUP BY c.moneda, t.tipo""", (f"{mes}%",))
    for row in flujo:
        mon, tipo, total = row["moneda"], row["tipo"], row["total"]
        if mon == "COP":
            cash_cop += total if tipo == "ingreso" else -total
        elif mon == "USD":
            cash_usd += total if tipo == "ingreso" else -total

    inv_total = sum(a["valor_actual"] or a["costo_base"] for a in activos)
    nw = inv_total + cash_usd + (cash_cop / tasa) - deudas

    return jsonify({
        "net_worth": round(nw, 2),
        "inv_total": round(inv_total, 2),
        "cash_usd": round(cash_usd, 2),
        "cash_cop": round(cash_cop, 2),
        "deudas": deudas,
        "tasa_cop": tasa,
        "activos": activos,
    })


@app.route("/api/inversiones/<tabla>")
def inversiones(tabla):
    if tabla not in ("inversiones_personal","inversiones_family","inversiones_papas"):
        return jsonify({"error":"tabla inválida"}),400
    return jsonify(q(f"SELECT * FROM {tabla} ORDER BY fecha DESC"))


@app.route("/api/inversiones/<tabla>/activos")
def inv_activos(tabla):
    if tabla not in ("inversiones_personal","inversiones_family","inversiones_papas"):
        return jsonify({"error":"tabla inválida"}),400
    precios = {x["activo"]: x["precio_usd"] for x in q("SELECT * FROM precios_mercado")}
    rows = q(f"""SELECT activo,
        SUM(monto_usd) as costo_base,
        SUM(CASE WHEN monto_usd>0 THEN cantidad ELSE -COALESCE(cantidad,0) END) as qty,
        COUNT(*) as ops
        FROM {tabla} GROUP BY activo ORDER BY costo_base DESC""")
    for r in rows:
        p = precios.get(r["activo"])
        r["precio_actual"] = p
        if p and r["qty"]:
            r["valor_actual"] = round(r["qty"] * p, 2)
            r["ganancia"] = round(r["valor_actual"] - r["costo_base"], 2)
            r["ganancia_pct"] = round(r["ganancia"] / r["costo_base"] * 100, 1) if r["costo_base"] else 0
        else:
            r["valor_actual"] = None; r["ganancia"] = None; r["ganancia_pct"] = None
    return jsonify(rows)


@app.route("/api/inversiones/eliminar", methods=["POST"])
def del_inv():
    d = request.json
    tabla = d.get("tabla","inversiones_personal")
    if tabla not in ("inversiones_personal","inversiones_family","inversiones_papas"):
        return jsonify({"error":"tabla inválida"}), 400
    ex(f"DELETE FROM {tabla} WHERE id=?", (d["id"],))
    return jsonify({"ok": True})

@app.route("/api/inversiones/editar", methods=["POST"])
def edit_inv():
    d = request.json
    tabla = d.get("tabla","inversiones_personal")
    if tabla not in ("inversiones_personal","inversiones_family","inversiones_papas"):
        return jsonify({"error":"tabla inválida"}), 400
    ex(f"""UPDATE {tabla} SET fecha=?,activo=?,tipo=?,monto_usd=?,cantidad=?,precio_unitario=?,notas=?
           WHERE id=?""",
       (d.get("fecha"), d.get("activo"), d.get("tipo"), d.get("monto_usd"),
        d.get("cantidad"), d.get("precio_unitario"), d.get("notas",""), d["id"]))
    return jsonify({"ok": True})

@app.route("/api/inversiones/agregar", methods=["POST"])
def add_inv():
    d = request.json
    tabla = d.get("tabla","inversiones_personal")
    if tabla not in ("inversiones_personal","inversiones_family","inversiones_papas"):
        return jsonify({"error":"tabla inválida"}),400
    monto = float(d["monto_usd"])
    if d.get("tipo","compra").lower() == "venta": monto = -abs(monto)
    ex(f"INSERT INTO {tabla} (fecha,activo,tipo,cantidad,precio_unitario,monto_usd,notas) VALUES(?,?,?,?,?,?,?)",
       (d.get("fecha",date.today().isoformat()), d["activo"].upper(),
        d.get("tipo","compra").lower(), d.get("cantidad") or None,
        d.get("precio_unitario") or None, monto, d.get("notas","")))
    activo = d["activo"].upper()
    precio = d.get("precio_unitario")
    if precio:
        ex("INSERT INTO precios_mercado(activo,precio_usd,actualizado_en) VALUES(?,?,?) ON CONFLICT(activo) DO UPDATE SET precio_usd=excluded.precio_usd,actualizado_en=excluded.actualizado_en",
           (activo, float(precio), date.today().isoformat()))
    return jsonify({"ok": True})


@app.route("/api/precios", methods=["GET"])
def precios(): return jsonify(q("SELECT * FROM precios_mercado ORDER BY activo"))

@app.route("/api/precios/actualizar", methods=["POST"])
def upd_precio():
    d = request.json
    ex("INSERT INTO precios_mercado(activo,precio_usd,actualizado_en) VALUES(?,?,?) ON CONFLICT(activo) DO UPDATE SET precio_usd=excluded.precio_usd,actualizado_en=excluded.actualizado_en",
       (d["activo"].upper(), float(d["precio_usd"]), date.today().isoformat()))
    return jsonify({"ok": True})

@app.route("/api/precios/live", methods=["GET"])
def precios_live():
    import urllib.request, json as _json, time
    now = date.today().isoformat()
    result = {}
    errors = []

    def fetch(url, headers={}):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **headers})
            with urllib.request.urlopen(req, timeout=8) as r:
                return _json.loads(r.read())
        except Exception as e:
            return None

    # BTC price (CoinGecko)
    cg = fetch("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,MicroStrategy&vs_currencies=usd")
    if cg and "bitcoin" in cg:
        btc = float(cg["bitcoin"]["usd"])
        result["BTC"] = btc
        ex("INSERT INTO precios_mercado(activo,precio_usd,actualizado_en) VALUES(?,?,?) ON CONFLICT(activo) DO UPDATE SET precio_usd=excluded.precio_usd,actualizado_en=excluded.actualizado_en",
           ("BTC", btc, now))
    else:
        errors.append("BTC")

    # MSTR price (Yahoo Finance)
    yf = fetch("https://query1.finance.yahoo.com/v8/finance/chart/MSTR?interval=1d&range=1d")
    if yf:
        try:
            mstr = float(yf["chart"]["result"][0]["meta"]["regularMarketPrice"])
            result["MSTR"] = mstr
            ex("INSERT INTO precios_mercado(activo,precio_usd,actualizado_en) VALUES(?,?,?) ON CONFLICT(activo) DO UPDATE SET precio_usd=excluded.precio_usd,actualizado_en=excluded.actualizado_en",
               ("MSTR", mstr, now))
        except:
            errors.append("MSTR")
    else:
        errors.append("MSTR")

    # USD/COP and USD/PYG (exchangerate-api free tier)
    fx = fetch("https://open.er-api.com/v6/latest/USD")
    if fx and fx.get("result") == "success":
        rates = fx["rates"]
        if "COP" in rates:
            cop = float(rates["COP"])
            result["USD_COP"] = cop
            ex("INSERT INTO config(clave,valor) VALUES(?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
               ("tasa_cop_usd", str(cop)))
        if "PYG" in rates:
            pyg = float(rates["PYG"])
            result["USD_PYG"] = pyg
            ex("INSERT INTO config(clave,valor) VALUES(?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
               ("tasa_pyg_usd", str(pyg)))
    else:
        errors.append("FX")

    return jsonify({"ok": True, "precios": result, "errors": errors, "actualizado": now})


@app.route("/api/budget/mes")
def budget_mes():
    mes = request.args.get("mes", datetime.now().strftime("%Y-%m"))
    return jsonify(q("""SELECT c.id,c.nombre,c.limite_mensual,c.tipo,c.moneda,
        COALESCE(SUM(t.monto),0) as gastado
        FROM categorias c
        LEFT JOIN transacciones t ON t.categoria_id=c.id AND t.fecha LIKE ?
        GROUP BY c.id ORDER BY gastado DESC""", (f"{mes}%",)))

@app.route("/api/budget/cash")
def budget_cash():
    """
    Liquidez POR CUENTA. Antes se agrupaba solo por moneda y un gasto en USD se
    repartía entre todas las cuentas en dólares, descontando de Cash aunque
    hubieras pagado con otra. Ahora cada gasto descuenta de la cuenta que elijas.
    """
    mes = request.args.get("mes", datetime.now().strftime("%Y-%m"))

    cuentas = q("SELECT id, descripcion, monto, moneda, cuenta FROM ahorros ORDER BY moneda, cuenta, descripcion")

    if not _tiene_columna("transacciones", "cuenta_id"):
        # Sin columna: no podemos saber de qué cuenta salió cada gasto todavía
        return jsonify([{
            "cuenta_id": c["id"],
            "nombre": (c.get("cuenta") or "").strip() or c["descripcion"],
            "descripcion": c["descripcion"],
            "moneda": c["moneda"],
            "saldo_ahorros": c["monto"],
            "ingresos_mes": 0, "gastos_mes": 0,
            "disponible": c["monto"],
        } for c in cuentas])

    # Movimientos del mes con la cuenta a la que se cargaron
    movs = q("""
        SELECT t.cuenta_id, c.moneda, t.tipo, COALESCE(SUM(t.monto),0) as total
        FROM transacciones t JOIN categorias c ON t.categoria_id=c.id
        WHERE t.fecha LIKE ? AND COALESCE(t.afecta_cash, 1) = 1
        GROUP BY t.cuenta_id, c.moneda, t.tipo
    """, (f"{mes}%",))

    def suma(cuenta_id, tipo, moneda=None):
        return sum(m["total"] for m in movs
                   if m["cuenta_id"] == cuenta_id and m["tipo"] == tipo
                   and (moneda is None or m["moneda"] == moneda))

    resultado = []
    for c in cuentas:
        ing = suma(c["id"], "ingreso")
        gas = suma(c["id"], "gasto")
        resultado.append({
            "cuenta_id": c["id"],
            # El nombre visible es dónde está guardado (GrabFi, Listo Global...);
            # la descripción queda como detalle secundario.
            "nombre": (c.get("cuenta") or "").strip() or c["descripcion"],
            "descripcion": c["descripcion"],
            "moneda": c["moneda"],
            "saldo_ahorros": c["monto"],
            "ingresos_mes": ing,
            "gastos_mes": gas,
            "disponible": c["monto"] + ing - gas,
        })

    # Movimientos sin cuenta asignada (los antiguos): se muestran aparte por
    # moneda para que no desaparezcan ni ensucien el saldo de una cuenta real.
    monedas_sueltas = sorted({m["moneda"] for m in movs if m["cuenta_id"] is None and m["moneda"]})
    for mon in monedas_sueltas:
        ing = suma(None, "ingreso", mon)
        gas = suma(None, "gasto", mon)
        if ing == 0 and gas == 0:
            continue
        resultado.append({
            "cuenta_id": None,
            "nombre": "Sin cuenta asignada",
            "moneda": mon,
            "saldo_ahorros": 0,
            "ingresos_mes": ing,
            "gastos_mes": gas,
            "disponible": ing - gas,
            "sin_asignar": True,
        })
    return jsonify(resultado)


@app.route("/api/budget/cerrar-mes", methods=["POST"])
def cerrar_mes():
    """
    Guarda el saldo con el que cierras el mes como saldo real de CADA cuenta.
    Manual a propósito: así nunca se resta dos veces.
    """
    data = request.get_json(force=True) or {}
    mes = data.get("mes") or datetime.now().strftime("%Y-%m")

    if not _tiene_columna("transacciones", "cuenta_id"):
        return jsonify({"ok": False, "error": "Falta la columna cuenta_id"}), 400
    movs = q("""
        SELECT t.cuenta_id, t.tipo, COALESCE(SUM(t.monto),0) as total
        FROM transacciones t JOIN categorias c ON t.categoria_id=c.id
        WHERE t.fecha LIKE ? AND COALESCE(t.afecta_cash, 1) = 1 AND t.cuenta_id IS NOT NULL
        GROUP BY t.cuenta_id, t.tipo
    """, (f"{mes}%",))

    actualizados = []
    for c in q("SELECT id, descripcion, monto, moneda, cuenta FROM ahorros"):
        ing = sum(m["total"] for m in movs if m["cuenta_id"] == c["id"] and m["tipo"] == "ingreso")
        gas = sum(m["total"] for m in movs if m["cuenta_id"] == c["id"] and m["tipo"] == "gasto")
        nuevo = c["monto"] + ing - gas
        if abs(nuevo - c["monto"]) < 0.01:
            continue
        ex("UPDATE ahorros SET monto=?, fecha_actualizacion=? WHERE id=?",
           (nuevo, date.today().isoformat(), c["id"]))
        actualizados.append({"cuenta": (c.get("cuenta") or "").strip() or c["descripcion"], "moneda": c["moneda"],
                             "antes": c["monto"], "ahora": nuevo})
    return jsonify({"ok": True, "mes": mes, "actualizados": actualizados})


@app.route("/api/budget/limpiar-mes", methods=["POST"])
def limpiar_mes():
    """Borra los movimientos de un mes para empezarlo de cero.
    No toca los saldos de ahorros: arriba seguirás viendo tu dinero real."""
    data = request.get_json(force=True) or {}
    mes = data.get("mes") or datetime.now().strftime("%Y-%m")
    antes = q("SELECT COUNT(*) as n FROM transacciones WHERE fecha LIKE ?", (f"{mes}%",))
    ex("DELETE FROM transacciones WHERE fecha LIKE ?", (f"{mes}%",))
    return jsonify({"ok": True, "mes": mes, "borrados": antes[0]["n"] if antes else 0})


@app.route("/api/budget/recientes")
def budget_recientes():
    mes = request.args.get("mes", datetime.now().strftime("%Y-%m"))
    if not _tiene_columna("transacciones", "cuenta_id"):
        # Sin la columna todavía: devolvemos lo básico en vez de reventar
        return jsonify(q("""SELECT t.id,t.fecha,t.monto,t.descripcion,t.tipo,
            t.categoria_id, c.nombre as categoria, c.moneda,
            NULL as cuenta_id, NULL as cuenta_nombre
            FROM transacciones t JOIN categorias c ON t.categoria_id=c.id
            WHERE t.fecha LIKE ? ORDER BY t.fecha DESC, t.id DESC LIMIT 60""", (f"{mes}%",)))
    return jsonify(q("""SELECT t.id,t.fecha,t.monto,t.descripcion,t.tipo,t.cuenta_id,
        t.categoria_id, c.nombre as categoria, c.moneda,
        COALESCE(NULLIF(TRIM(a.cuenta),''), a.descripcion) as cuenta_nombre
        FROM transacciones t
        JOIN categorias c ON t.categoria_id=c.id
        LEFT JOIN ahorros a ON a.id=t.cuenta_id
        WHERE t.fecha LIKE ? ORDER BY t.fecha DESC, t.id DESC LIMIT 60""", (f"{mes}%",)))

@app.route("/api/transacciones/eliminar", methods=["POST"])
def del_tx():
    ex("DELETE FROM transacciones WHERE id=?", (request.json["id"],))
    return jsonify({"ok": True})

@app.route("/api/transacciones/editar", methods=["POST"])
def edit_tx():
    """Edita un movimiento ya guardado: fecha, monto, categoría, cuenta y nota."""
    d = request.json
    tid = int(d["id"])
    campos, valores = [], []

    if d.get("categoria"):
        cat = d["categoria"]
        r = q("SELECT id FROM categorias WHERE nombre=?", (cat,))
        if not r:
            ex("INSERT INTO categorias(nombre,tipo) VALUES(?,?)", (cat, d.get("tipo", "gasto")))
            r = q("SELECT id FROM categorias WHERE nombre=?", (cat,))
        campos.append("categoria_id=?"); valores.append(r[0]["id"])

    if d.get("fecha"):
        campos.append("fecha=?"); valores.append(d["fecha"])
    if d.get("monto") not in (None, ""):
        campos.append("monto=?"); valores.append(float(d["monto"]))
    if "descripcion" in d:
        campos.append("descripcion=?"); valores.append(d.get("descripcion", ""))
    if d.get("tipo"):
        campos.append("tipo=?"); valores.append(d["tipo"])
    if "cuenta_id" in d and _tiene_columna("transacciones", "cuenta_id"):
        cta = d.get("cuenta_id")
        campos.append("cuenta_id=?")
        valores.append(int(cta) if cta not in (None, "", "null") else None)

    if campos:
        valores.append(tid)
        ex(f"UPDATE transacciones SET {', '.join(campos)} WHERE id=?", tuple(valores))
    return jsonify({"ok": True})


@app.route("/api/transacciones/agregar", methods=["POST"])
def add_tx():
    d = request.json
    cat = d["categoria"]; tipo = d.get("tipo","gasto")
    r = q("SELECT id FROM categorias WHERE nombre=?", (cat,))
    if not r: ex("INSERT INTO categorias(nombre,tipo) VALUES(?,?)", (cat, tipo))
    cid = q("SELECT id FROM categorias WHERE nombre=?", (cat,))[0]["id"]
    cuenta = d.get("cuenta_id")
    cuenta = int(cuenta) if cuenta not in (None, "", "null") else None
    if _tiene_columna("transacciones", "cuenta_id"):
        ex("INSERT INTO transacciones(fecha,categoria_id,monto,descripcion,tipo,cuenta_id) VALUES(?,?,?,?,?,?)",
           (d.get("fecha",date.today().isoformat()), cid, float(d["monto"]), d.get("descripcion",""), tipo, cuenta))
    else:
        ex("INSERT INTO transacciones(fecha,categoria_id,monto,descripcion,tipo) VALUES(?,?,?,?,?)",
           (d.get("fecha",date.today().isoformat()), cid, float(d["monto"]), d.get("descripcion",""), tipo))
    return jsonify({"ok": True})

@app.route("/api/categorias")
def categorias(): return jsonify(q("SELECT * FROM categorias ORDER BY nombre"))

@app.route("/api/categorias/agregar", methods=["POST"])
def add_cat():
    d = request.json
    ex("INSERT OR IGNORE INTO categorias(nombre,limite_mensual,tipo,moneda) VALUES(?,?,?,?)",
       (d["nombre"], d.get("limite_mensual") or None, d.get("tipo","gasto"), d.get("moneda","USD")))
    return jsonify({"ok": True})

@app.route("/api/categorias/actualizar", methods=["POST"])
def upd_cat():
    d = request.json
    ex("UPDATE categorias SET nombre=?,limite_mensual=?,tipo=?,moneda=? WHERE id=?",
       (d["nombre"], d.get("limite_mensual") or None, d.get("tipo","gasto"), d.get("moneda","USD"), d["id"]))
    return jsonify({"ok": True})

@app.route("/api/categorias/eliminar", methods=["POST"])
def del_cat():
    d = request.json
    cid = d["id"]
    ex("DELETE FROM transacciones WHERE categoria_id=?", (cid,))
    ex("DELETE FROM categorias WHERE id=?", (cid,))
    return jsonify({"ok": True})

@app.route("/api/ahorros")
def ahorros():
    tasa_cop = float(cfg("tasa_cop_usd") or 4050)
    tasa_pyg = float(cfg("tasa_pyg_usd") or 7300)
    rows = q("SELECT * FROM ahorros")
    def to_usd(r):
        m, amt = r["moneda"], r["monto"]
        if m == "COP": return amt / tasa_cop
        if m == "PYG": return amt / tasa_pyg
        return amt  # USD, EUR, etc treated as 1:1 for sorting
    rows.sort(key=to_usd, reverse=True)
    return jsonify(rows)

@app.route("/api/ahorros/agregar", methods=["POST"])
def add_ahorro():
    d = request.json
    ex("INSERT INTO ahorros(descripcion,monto,moneda,fecha_actualizacion,notas,cuenta) VALUES(?,?,?,?,?,?)",
       (d["descripcion"], float(d["monto"]), d.get("moneda","USD"), date.today().isoformat(),
        d.get("notas",""), d.get("cuenta","")))
    return jsonify({"ok": True})

@app.route("/api/ahorros/actualizar", methods=["POST"])
def upd_ahorro():
    d = request.json
    ex("UPDATE ahorros SET descripcion=?,monto=?,moneda=?,cuenta=?,fecha_actualizacion=? WHERE id=?",
       (d.get("descripcion"), float(d["monto"]), d.get("moneda","USD"), d.get("cuenta",""), date.today().isoformat(), d["id"]))
    return jsonify({"ok": True})

@app.route("/api/ahorros/eliminar", methods=["POST"])
def del_ahorro():
    ex("DELETE FROM ahorros WHERE id=?", (request.json["id"],))
    return jsonify({"ok": True})

def _meses_entre(a, b):
    """Lista de meses YYYY-MM desde a hasta b inclusive (máx 120)."""
    y, mth = int(a[:4]), int(a[5:7])
    out = []
    while f"{y:04d}-{mth:02d}" <= b and len(out) < 120:
        out.append(f"{y:04d}-{mth:02d}")
        mth += 1
        if mth == 13: mth = 1; y += 1
    return out

def _sumar_meses(mes, n):
    y, mth = int(mes[:4]), int(mes[5:7])
    mth += n
    y += (mth - 1) // 12
    mth = (mth - 1) % 12 + 1
    return f"{y:04d}-{mth:02d}"

@app.route("/api/metas")
def metas():
    rows = q("SELECT * FROM metas WHERE activa=1 ORDER BY fecha_objetivo")
    today = date.today()
    for m in rows:
        if m.get("tipo") == "btc":
            desde = m.get("btc_desde") or "2026-07-01"
            res = q("""SELECT COALESCE(SUM(t),0) as total FROM (
                SELECT monto_usd as t FROM inversiones_personal WHERE activo='BTC' AND monto_usd>0 AND fecha>=?
                UNION ALL
                SELECT monto_usd as t FROM inversiones_family WHERE activo='BTC' AND monto_usd>0 AND fecha>=?
            )""", (desde, desde))
            m["monto_actual"] = round(res[0]["total"], 2)
        elif m.get("tipo") == "cash":
            res = q("SELECT COALESCE(SUM(monto),0) as total FROM meta_depositos WHERE meta_id=?", (m["id"],))
            m["monto_actual"] = round(res[0]["total"], 2)
        if m["fecha_objetivo"] and m["monto_objetivo"]:
            t = date.fromisoformat(m["fecha_objetivo"])
            meses = max(1,(t.year-today.year)*12+t.month-today.month)
            falta = m["monto_objetivo"] - m["monto_actual"]
            m["por_mes"] = round(falta/meses, 2)
            m["pct"] = round(m["monto_actual"]/m["monto_objetivo"]*100, 1) if m["monto_objetivo"] else 0

        # Desglose mensual: cuánto entró cada mes (cash: depósitos, btc: compras)
        if m["fecha_objetivo"] and m["monto_objetivo"] and m.get("tipo") in ("cash","btc"):
            if m["tipo"] == "btc":
                desde = m.get("btc_desde") or "2026-07-01"
                entradas = q("""SELECT NULL as id, fecha, monto_usd as monto, origen, '' as cuenta, notas FROM (
                    SELECT fecha, monto_usd, 'Personal' as origen, COALESCE(notas,'') as notas
                        FROM inversiones_personal WHERE activo='BTC' AND monto_usd>0 AND fecha>=?
                    UNION ALL
                    SELECT fecha, monto_usd, 'Family' as origen, COALESCE(notas,'') as notas
                        FROM inversiones_family WHERE activo='BTC' AND monto_usd>0 AND fecha>=?
                ) ORDER BY fecha""", (desde, desde))
                inicio = desde[:7]
            else:
                entradas = q("""SELECT id, fecha, monto, '' as origen, COALESCE(cuenta,'') as cuenta,
                    COALESCE(notas,'') as notas FROM meta_depositos WHERE meta_id=? ORDER BY fecha""", (m["id"],))
                primer = min((e["fecha"][:7] for e in entradas), default=None)
                creado = (m.get("creado_en") or today.isoformat())[:7]
                inicio = min(primer, creado) if primer else creado
            dep = {}
            for e in entradas:
                dep[e["fecha"][:7]] = dep.get(e["fecha"][:7], 0) + e["monto"]
            dep = [{"mes": k, "total": v} for k, v in dep.items()]
            mes_hoy = today.strftime("%Y-%m")
            inicio = min(inicio, mes_hoy)
            meses_lista = _meses_entre(inicio, m["fecha_objetivo"][:7])
            if not meses_lista:
                meses_lista = [mes_hoy]
            dep_map = {d["mes"]: d["total"] for d in dep}
            req_plan = m["monto_objetivo"] / len(meses_lista)
            transcurridos = [x for x in meses_lista if x <= mes_hoy]
            ritmo = sum(dep_map.get(x, 0) for x in transcurridos) / max(1, len(transcurridos))
            falta = max(0, m["monto_objetivo"] - m["monto_actual"])
            meses_ritmo = math.ceil(falta / ritmo) if ritmo > 0 and falta > 0 else None
            m["plan"] = {
                "inicio": inicio,
                "mes_actual": mes_hoy,
                "ahorrado_mes": round(dep_map.get(mes_hoy, 0), 2),
                "req_mes_plan": round(req_plan, 2),
                "ritmo": round(ritmo, 2),
                "fin_estimado": _sumar_meses(mes_hoy, meses_ritmo) if meses_ritmo else (mes_hoy if falta == 0 else None),
                "meses": [{"mes": x, "ahorrado": round(dep_map.get(x, 0), 2)} for x in meses_lista],
                "entradas": entradas,
            }
    return jsonify(rows)

@app.route("/api/metas/agregar", methods=["POST"])
def add_meta():
    d = request.json
    tipo = d.get("tipo", "manual")
    ex("INSERT INTO metas(nombre,monto_objetivo,fecha_objetivo,monto_actual,notas,tipo,btc_desde) VALUES(?,?,?,?,?,?,?)",
       (d["nombre"], float(d["monto_objetivo"]), d.get("fecha_objetivo"), 0, d.get("notas",""), tipo, d.get("btc_desde")))
    return jsonify({"ok": True})

@app.route("/api/metas/depositos/agregar", methods=["POST"])
def add_deposito():
    d = request.json
    ex("INSERT INTO meta_depositos(meta_id,monto,moneda,cuenta,fecha,notas) VALUES(?,?,?,?,?,?)",
       (d["meta_id"], float(d["monto"]), d.get("moneda","USD"), d.get("cuenta",""), d.get("fecha", date.today().isoformat()), d.get("notas","")))
    return jsonify({"ok": True})

@app.route("/api/metas/depositos/eliminar", methods=["POST"])
def del_deposito():
    ex("DELETE FROM meta_depositos WHERE id=?", (request.json["id"],))
    return jsonify({"ok": True})

@app.route("/api/metas/<int:meta_id>/depositos")
def get_depositos(meta_id):
    return jsonify(q("SELECT * FROM meta_depositos WHERE meta_id=? ORDER BY fecha DESC", (meta_id,)))

@app.route("/api/metas/depositos/editar", methods=["POST"])
def edit_deposito():
    d = request.json
    ex("UPDATE meta_depositos SET monto=?, cuenta=?, fecha=?, notas=? WHERE id=?",
       (float(d["monto"]), d.get("cuenta",""), d["fecha"], d.get("notas",""), d["id"]))
    return jsonify({"ok": True})

@app.route("/api/metas/editar", methods=["POST"])
def edit_meta():
    d = request.json
    ex("UPDATE metas SET nombre=?,monto_objetivo=?,fecha_objetivo=?,tipo=?,btc_desde=?,notas=? WHERE id=?",
       (d["nombre"], float(d["monto_objetivo"]), d.get("fecha_objetivo"), d.get("tipo","manual"), d.get("btc_desde"), d.get("notas",""), d["id"]))
    return jsonify({"ok": True})

@app.route("/api/metas/eliminar", methods=["POST"])
def del_meta():
    ex("UPDATE metas SET activa=0 WHERE id=?", (request.json["id"],))
    return jsonify({"ok": True})

@app.route("/api/config", methods=["GET"])
def get_config(): return jsonify({x["clave"]:x["valor"] for x in q("SELECT * FROM config")})

@app.route("/api/config/actualizar", methods=["POST"])
def upd_config():
    d = request.json
    for k,v in d.items():
        ex("INSERT INTO config(clave,valor) VALUES(?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (k,str(v)))
    return jsonify({"ok": True})


# ── Dashboard ─────────────────────────────────────────────────────────────────

DASH = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f7931a">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="CFO">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/static/icon-192.png">
<title>CFO Personal</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='85' fill='%23f7931a'>₿</text></svg>">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root{
  --bg:#f2f2f7;--surface:#fff;--surface2:#f9f9fb;
  --border:#e5e5ea;--border2:rgba(0,0,0,0.06);
  --shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);
  --shadow-lg:0 8px 24px rgba(0,0,0,.08);
  --text:#1d1d1f;--text2:#6e6e73;--text3:#aeaeb2;
  --accent:#6366f1;--accent2:#818cf8;--accent-bg:#eef2ff;
  --green:#059669;--green-bg:#ecfdf5;
  --red:#dc2626;--red-bg:#fef2f2;
  --amber:#d97706;--amber-bg:#fffbeb;
  --blue:#2563eb;--blue-bg:#eff6ff;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',-apple-system,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;-webkit-font-smoothing:antialiased}

/* Header */
.header{background:rgba(255,255,255,.85);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);padding:0 24px;position:sticky;top:0;z-index:100;
  display:flex;align-items:center;justify-content:space-between;height:56px}
.header-brand{display:flex;align-items:center;gap:10px}
.header-logo{width:28px;height:28px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border-radius:8px;
  display:flex;align-items:center;justify-content:center;color:#fff;font-size:14px;font-weight:700}
.header-title{font-weight:700;font-size:16px;color:var(--text)}
.header-date{font-size:12px;color:var(--text3)}

/* Nav */
.nav{display:flex;gap:2px;padding:16px 24px 0;overflow-x:auto}
.nav-btn{background:none;border:none;padding:8px 14px;border-radius:8px;font-size:13px;font-weight:500;
  color:var(--text2);cursor:pointer;white-space:nowrap;transition:all .15s;font-family:inherit}
.nav-btn:hover{background:var(--border);color:var(--text)}
.nav-btn.active{background:var(--accent);color:#fff}

/* Main */
main{padding:20px 32px 48px;max-width:1600px;width:100%}

/* Cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.card{background:var(--surface);border-radius:14px;padding:18px;box-shadow:var(--shadow);border:1px solid var(--border2)}
.card-lbl{font-size:11px;font-weight:500;color:var(--text3);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px}
.card-val{font-size:24px;font-weight:700;color:var(--text);letter-spacing:-.5px}
.card-sub{font-size:11px;color:var(--text3);margin-top:4px}
.card-change{display:inline-flex;align-items:center;gap:3px;font-size:11px;font-weight:600;
  padding:2px 7px;border-radius:6px;margin-top:6px}
.card-change.up{background:var(--green-bg);color:var(--green)}
.card-change.dn{background:var(--red-bg);color:var(--red)}

/* Hero */
.hero{background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 50%,#a78bfa 100%);
  border-radius:20px;padding:28px 32px;color:#fff;margin-bottom:24px;
  box-shadow:0 8px 32px rgba(99,102,241,.25)}
.hero-lbl{font-size:12px;font-weight:500;opacity:.75;text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px}
.hero-val{font-size:44px;font-weight:800;letter-spacing:-1.5px;line-height:1}
.hero-sub{font-size:13px;opacity:.7;margin-top:10px}

/* Grids */
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px;align-items:start}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin-bottom:24px}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:24px}
.box{background:var(--surface);border-radius:14px;padding:20px;box-shadow:var(--shadow);border:1px solid var(--border2)}
.box-title{font-size:12px;font-weight:600;color:var(--text2);text-transform:uppercase;letter-spacing:.6px;margin-bottom:16px}
canvas{max-height:220px}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:13px}
th{padding:8px 12px;text-align:left;font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;
  letter-spacing:.5px;border-bottom:1px solid var(--border)}
td{padding:11px 12px;border-bottom:1px solid var(--border2);color:var(--text)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg)}
.tbl-wrap{background:var(--surface);border-radius:14px;overflow:hidden;box-shadow:var(--shadow);
  border:1px solid var(--border2);margin-bottom:20px}

/* Badges */
.badge{display:inline-flex;align-items:center;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600}
.badge-green{background:var(--green-bg);color:var(--green)}
.badge-red{background:var(--red-bg);color:var(--red)}
.badge-blue{background:var(--blue-bg);color:var(--blue)}
.badge-accent{background:var(--accent-bg);color:var(--accent)}

/* Asset grid — multi-asset side by side */
.asset-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:12px;margin-bottom:4px}

/* Asset card */
.asset-card{background:var(--surface);border-radius:14px;padding:20px;box-shadow:var(--shadow);
  border:1px solid var(--border2);cursor:pointer;transition:.15s}
.asset-card:hover{box-shadow:var(--shadow-lg)}
.asset-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.asset-name{font-size:18px;font-weight:700;letter-spacing:-.3px}
.asset-qty{font-size:13px;color:var(--text2)}
.asset-numbers{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.asset-num .lbl{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px}
.asset-num .val{font-size:15px;font-weight:700}
.asset-detail{margin-top:14px;border-top:1px solid var(--border);padding-top:14px;display:none;overflow-x:auto}

/* Forms */
.form-card{background:var(--surface);border-radius:14px;padding:20px;box-shadow:var(--shadow);
  border:1px solid var(--border2);margin-bottom:20px}
.form-title{font-size:13px;font-weight:600;color:var(--text2);margin-bottom:14px}
.form-row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.f-group{display:flex;flex-direction:column;gap:4px}
.f-group label{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.f-group input,.f-group select{background:var(--bg);border:1.5px solid var(--border);border-radius:9px;
  color:var(--text);padding:8px 12px;font-size:13px;font-family:inherit;outline:none;
  min-width:110px;transition:.15s}
.f-group input:focus,.f-group select:focus{border-color:var(--accent);background:#fff}
.btn{padding:9px 18px;border-radius:9px;font-size:13px;font-weight:600;cursor:pointer;
  border:none;font-family:inherit;transition:.15s}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:#5558e3}
.btn-sm{padding:6px 12px;font-size:12px;border-radius:7px}
.btn-outline{background:none;border:1.5px solid var(--border);color:var(--text2)}
.btn-outline:hover{border-color:var(--accent);color:var(--accent)}

/* Budget bars */
.budget-item{margin-bottom:2px;border-radius:10px;padding:10px 10px 8px;transition:.15s}
.budget-item:hover{background:var(--bg)}
.budget-meta{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px}
.budget-name{font-size:13px;font-weight:500;flex:1}
.budget-amounts{font-size:12px;color:var(--text3)}
.budget-actions{display:flex;gap:4px;opacity:0;transition:.15s}
.budget-item:hover .budget-actions{opacity:1}
.budget-action-btn{background:none;border:none;cursor:pointer;padding:3px 5px;border-radius:5px;font-size:12px;color:var(--text3);font-family:inherit}
.budget-action-btn:hover{background:var(--border);color:var(--text)}
.budget-action-btn.del:hover{background:var(--red-bg);color:var(--red)}
.bar-track{height:7px;background:var(--border);border-radius:4px;overflow:hidden}
.bar-fill{height:7px;border-radius:4px;transition:.4s}
.bar-green{background:var(--green)}
.bar-amber{background:var(--amber)}
.bar-red{background:var(--red)}
/* Inline edit form for a category */
.cat-edit-form{background:var(--accent-bg);border-radius:10px;padding:12px;margin-top:6px;display:none}
.cat-edit-form .form-row{gap:8px;flex-wrap:wrap}
.cat-edit-form input,.cat-edit-form select{padding:6px 10px;font-size:12px;min-width:90px}

/* Cash flow panel */
.cash-panel{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;margin-bottom:20px}
.cash-card{background:var(--surface);border-radius:14px;padding:18px;box-shadow:var(--shadow);
  border:1px solid var(--border2);position:relative;overflow:hidden}
.cash-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.cash-card.usd::before{background:linear-gradient(90deg,#2563eb,#60a5fa)}
.cash-card.cop::before{background:linear-gradient(90deg,#059669,#34d399)}
.cash-card.pyg::before{background:linear-gradient(90deg,#d97706,#fbbf24)}
.cash-card.eur::before{background:linear-gradient(90deg,#7c3aed,#a78bfa)}
.cash-card.other::before{background:linear-gradient(90deg,#6366f1,#818cf8)}
.cash-moneda{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--text3);margin-bottom:8px}
.cash-disponible{font-size:26px;font-weight:800;letter-spacing:-.5px;margin-bottom:10px}
.cash-rows{display:flex;flex-direction:column;gap:4px}
.cash-row{display:flex;justify-content:space-between;align-items:center;font-size:12px}
.cash-row-lbl{color:var(--text3)}
.cash-row-val{font-weight:600}
.cash-row-val.in{color:var(--green)}
.cash-row-val.out{color:var(--red)}
.cash-row-val.base{color:var(--text2)}
.cash-divider{height:1px;background:var(--border);margin:6px 0}

/* Quick add */
.quick-add{display:flex;gap:8px;align-items:center;background:var(--surface);
  border-radius:14px;padding:14px 18px;box-shadow:var(--shadow);border:1px solid var(--border2);margin-bottom:20px}
.quick-add input,.quick-add select{flex:1;background:var(--bg);border:1.5px solid var(--border);
  border-radius:9px;padding:9px 12px;font-size:14px;font-family:inherit;outline:none;color:var(--text)}
.quick-add input:focus,.quick-add select:focus{border-color:var(--accent)}

/* Meta */
.meta-item{background:var(--surface);border-radius:12px;padding:16px;margin-bottom:10px;
  box-shadow:var(--shadow);border:1px solid var(--border2)}
.meta-top{display:flex;justify-content:space-between;margin-bottom:8px}
.meta-name{font-weight:600;font-size:14px}
.meta-pct{font-size:13px;font-weight:700;color:var(--accent)}
.meta-bar{height:8px;background:var(--bg);border-radius:4px;margin-bottom:6px}
.meta-fill{height:8px;border-radius:4px;background:linear-gradient(90deg,#6366f1,#8b5cf6)}
.meta-sub{font-size:11px;color:var(--text3)}

/* Ahorro item */
.ahorro-item{display:flex;justify-content:space-between;align-items:center;
  padding:13px 0;border-bottom:1px solid var(--border2)}
.ahorro-item:last-child{border-bottom:none}
.ahorro-lbl{font-size:14px;font-weight:500}
.ahorro-val{font-size:16px;font-weight:700;color:var(--blue)}

/* Section title */
.section-title{font-size:13px;font-weight:600;color:var(--text2);text-transform:uppercase;
  letter-spacing:.5px;margin:24px 0 12px}

/* Onboarding modal */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.4);backdrop-filter:blur(8px);
  z-index:999;display:flex;align-items:center;justify-content:center}
.modal{background:#fff;border-radius:24px;padding:36px;width:100%;max-width:440px;
  box-shadow:0 24px 64px rgba(0,0,0,.15)}
.modal h2{font-size:24px;font-weight:800;letter-spacing:-.5px;margin-bottom:6px}
.modal p{font-size:14px;color:var(--text2);margin-bottom:24px;line-height:1.5}
.modal .steps{display:flex;gap:6px;margin-bottom:28px}
.modal .step{height:4px;flex:1;border-radius:2px;background:var(--border)}
.modal .step.done{background:var(--accent)}
.modal .f-group{margin-bottom:14px;width:100%}
.modal .f-group input,.modal .f-group select{width:100%}
.modal .btn-primary{width:100%;padding:12px;font-size:15px;border-radius:12px}

/* tab content */
.tab{display:none}.tab.active{display:block}

/* responsive */
@media(max-width:900px){
  .grid3{grid-template-columns:1fr 1fr}
  .grid4{grid-template-columns:1fr 1fr}
}
@media(max-width:640px){
  .grid2,.grid3,.grid4{grid-template-columns:1fr}
  .asset-numbers{grid-template-columns:1fr 1fr}
  .hero-val{font-size:32px}
  main{padding:16px 16px 48px}
  .nav{padding:12px 16px 0}
  .header{padding:0 16px}
}

/* ── Inputs de fecha ──
   Safari/iOS les da un ancho intrínseco propio y se salían de su caja.
   appearance:none + min-width:0 hace que respeten el contenedor. */
input[type="date"],
input[type="month"],
input[type="time"]{
  -webkit-appearance:none;
  appearance:none;
  min-width:0;
  max-width:100%;
  box-sizing:border-box;
}
.f-group input[type="date"]{width:100%;min-width:0}
/* Safari centra el texto del date y descuadra la altura: lo alineamos */
input[type="date"]::-webkit-date-and-time-value{text-align:left;margin:0}
input[type="date"]::-webkit-calendar-picker-indicator{opacity:.55;cursor:pointer}

/* ── Navegador de mes ── */
.month-nav{display:flex;align-items:center;gap:4px}
.month-btn{background:var(--bg);border:1px solid var(--border);border-radius:8px;
  width:28px;height:28px;font-size:16px;line-height:1;color:var(--text2);cursor:pointer;
  font-family:inherit;display:flex;align-items:center;justify-content:center;padding:0}
.month-btn:hover{background:var(--border);color:var(--text)}
.month-hoy{width:auto;padding:0 10px;font-size:11px;font-weight:600}
#mes-picker{background:var(--bg);border:1px solid var(--border);border-radius:8px;
  padding:5px 8px;font-size:12px;font-family:inherit;color:var(--text);outline:none;
  min-width:130px}
#mes-picker:focus{border-color:var(--accent)}
.mes-viejo{background:var(--amber-bg)!important;border-color:var(--amber)!important}

/* ── Fixes móvil ── */
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tbl-wrap table{min-width:520px}
.asset-grid{grid-template-columns:repeat(auto-fill,minmax(min(100%,480px),1fr))}
.asset-detail{-webkit-overflow-scrolling:touch}
.nav{scrollbar-width:none}.nav::-webkit-scrollbar{display:none}
.header{padding-top:env(safe-area-inset-top);height:calc(56px + env(safe-area-inset-top))}
.modal-bg{padding:16px}
.modal{max-height:90dvh;overflow-y:auto}
@media(hover:none){
  .budget-actions{opacity:1}
}
@media(max-width:640px){
  .header{padding:0 12px}
  .header-title{display:none}
  #mes-picker{min-width:112px;font-size:11px;padding:4px 6px}
  .quick-add{flex-wrap:wrap;padding:12px 14px}
  .quick-add input,.quick-add select{flex:1 1 45%;min-width:0}
  .quick-add .btn{flex:1 1 100%;padding:12px}
  .form-row .f-group{flex:1 1 45%;min-width:0}
  .form-row .f-group input[type="date"]{width:100%}
  .quick-add input[type="date"]{flex:1 1 100%!important}
  .form-row .f-group input,.form-row .f-group select{width:100%;min-width:0}
  .form-row .btn{flex:1 1 100%;padding:12px}
  input,select{font-size:16px!important}
  .hero{padding:22px 20px;border-radius:16px}
  .modal{padding:24px;border-radius:18px}
  th,td{padding:8px 10px}
  .card-val{font-size:20px}
  .cash-disponible{font-size:22px}
  .asset-name{font-size:16px}
  main{padding-bottom:calc(48px + env(safe-area-inset-bottom))}
}
</style>
</head>
<body>

<!-- ── HEADER ── -->
<div class="header">
  <div class="header-brand">
    <div class="header-logo">C</div>
    <span class="header-title">CFO Personal</span>
  </div>
  <div class="month-nav">
    <button class="month-btn" onclick="cambiarMes(-1)" aria-label="Mes anterior">‹</button>
    <input type="month" id="mes-picker" onchange="setMes(this.value)">
    <button class="month-btn" onclick="cambiarMes(1)" aria-label="Mes siguiente">›</button>
    <button class="month-btn month-hoy" onclick="setMes(mesActual())" id="btn-hoy">Hoy</button>
  </div>
</div>

<!-- ── NAV ── -->
<div class="nav">
  <button class="nav-btn active" onclick="goto('home',this)">Inicio</button>
  <button class="nav-btn" onclick="goto('inv',this)">Inversiones</button>
  <button class="nav-btn" onclick="goto('budget',this)">Budget</button>
  <button class="nav-btn" onclick="goto('ahorros',this)">Ahorros & Metas</button>
  <button class="nav-btn" onclick="goto('config',this)">Ajustes</button>
</div>

<main>

<!-- ── INICIO ── -->
<div class="tab active" id="tab-home">
  <div class="hero">
    <div class="hero-lbl">Patrimonio neto</div>
    <div class="hero-val" id="nw-val">Cargando…</div>
    <div class="hero-sub" id="nw-sub"></div>
  </div>
  <div class="grid4" id="home-cards"></div>
  <div class="grid3">
    <div class="box"><div class="box-title">Distribución de activos</div><canvas id="c-activos"></canvas></div>
    <div class="box"><div class="box-title">Budget — gastos este mes</div><canvas id="c-budget"></canvas></div>
    <div class="box"><div class="box-title">Por portafolio</div><canvas id="c-portfolio"></canvas></div>
  </div>
  <div class="grid2">
    <div class="box"><div class="box-title">Budget: límite vs gastado</div><canvas id="c-budgetbar"></canvas></div>
    <div class="box"><div class="box-title">Liquidez — cuentas FIAT</div><div id="home-cash"></div></div>
  </div>
</div>

<!-- ── INVERSIONES ── -->
<div class="tab" id="tab-inv">
  <div class="form-card">
    <div class="form-title">Registrar operación</div>
    <div class="form-row">
      <div class="f-group"><label>Portafolio</label>
        <select id="i-tabla"><option value="inversiones_personal">Personal</option>
          <option value="inversiones_family">Family</option>
          <option value="inversiones_papas">Papás</option></select></div>
      <div class="f-group"><label>Tipo</label>
        <select id="i-tipo"><option value="compra">Compra</option><option value="venta">Venta</option></select></div>
      <div class="f-group"><label>Activo</label><input id="i-activo" placeholder="BTC" style="text-transform:uppercase"></div>
      <div class="f-group" style="flex:0 1 150px;min-width:0"><label>Fecha</label><input id="i-fecha" type="date"></div>
      <div class="f-group"><label>Monto USD</label><input id="i-monto" type="text" inputmode="decimal" data-money placeholder="500"></div>
      <div class="f-group"><label>Cantidad</label><input id="i-qty" type="number" step="0.00000001" placeholder="0.005"></div>
      <div class="f-group"><label>Precio por unidad</label><input id="i-precio" type="text" inputmode="decimal" data-money placeholder="100,000"></div>
      <div class="f-group"><label>Notas</label><input id="i-notas" placeholder="opcional" style="min-width:160px"></div>
      <div class="f-group"><label>Contar en meta</label><select id="i-meta"><option value="">Sin meta</option></select></div>
      <button class="btn btn-primary" onclick="addInv()">+ Agregar</button>
    </div>
  </div>

  <div class="section-title">Personal</div>
  <div class="asset-grid" id="inv-personal"></div>
  <div class="section-title">Family</div>
  <div class="asset-grid" id="inv-family"></div>
  <div class="section-title">Papás</div>
  <div class="asset-grid" id="inv-papas"></div>
</div>

<!-- ── BUDGET ── -->
<div class="tab" id="tab-budget">
  <div class="cash-panel" id="budget-cash"></div>
  <div id="acciones-mes" style="margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap">
    <button class="btn btn-outline" style="flex:1;min-width:150px" onclick="cerrarMes()" id="btn-cerrar">
      📌 Guardar saldo final
    </button>
    <button class="btn btn-outline" style="flex:1;min-width:150px" onclick="limpiarMes()" id="btn-limpiar">
      🗑 Empezar mes de cero
    </button>
  </div>
  <div class="quick-add">
    <select id="qa-cat" style="flex:1.5"></select>
    <select id="qa-cuenta" style="flex:1.2"></select>
    <input id="qa-monto" type="text" inputmode="decimal" data-money placeholder="Monto gastado">
    <input id="qa-desc" placeholder="Descripción (opcional)">
    <input id="qa-fecha" type="date" style="flex:0 1 150px;min-width:0">
    <button class="btn btn-primary" onclick="addGasto()">+ Gasto</button>
  </div>

  <div class="grid2">
    <div class="box">
      <div class="box-title">Categorías — <span id="mes-label"></span></div>
      <div id="budget-bars"></div>
    </div>
    <div class="box">
      <div class="box-title">Últimas transacciones</div>
      <div id="budget-recientes"></div>
    </div>
  </div>

  <div class="section-title">Nueva categoría</div>
  <div class="form-card" style="margin-top:0">
    <div class="form-row">
      <div class="f-group"><label>Nombre</label><input id="nc-nombre" placeholder="Restaurantes"></div>
      <div class="f-group"><label>Límite mensual</label><input id="nc-limite" type="text" inputmode="decimal" data-money placeholder="500"></div>
      <div class="f-group"><label>Moneda</label>
        <select id="nc-moneda">
          <option value="USD">USD $</option>
          <option value="COP">COP $</option>
          <option value="PYG">PYG ₲</option>
          <option value="EUR">EUR €</option>
          <option value="ARS">ARS $</option>
        </select></div>
      <div class="f-group"><label>Tipo</label>
        <select id="nc-tipo"><option value="gasto">Gasto</option><option value="ahorro">Ahorro</option><option value="ingreso">Ingreso</option></select></div>
      <button class="btn btn-primary" onclick="addCat()">+ Agregar</button>
    </div>
  </div>
</div>

<!-- ── AHORROS ── -->
<div class="tab" id="tab-ahorros">
  <div class="grid2">
    <div class="box">
      <div class="box-title">Efectivo & cuentas</div>
      <div id="ahorros-list"></div>
      <div id="ahorros-total" style="border-top:2px solid var(--border);margin-top:8px;padding-top:10px"></div>
      <div class="form-row" style="margin-top:16px">
        <div class="f-group"><label>Descripción / Cuenta</label><input id="a-desc" placeholder="Listo Global"></div>
        <div class="f-group"><label>Monto</label><input id="a-monto" type="text" inputmode="decimal" data-money placeholder="5,000"></div>
        <div class="f-group"><label>Moneda</label>
          <select id="a-moneda"><option value="USD">USD</option><option value="COP">COP</option><option value="PYG">PYG ₲</option></select></div>
        <div class="f-group"><label>Tipo de cuenta</label>
          <select id="a-cuenta">
            <option value="GrabFi">GrabFi</option>
            <option value="Listo Global">Listo Global</option>
            <option value="Cuenta Paraguay">Cuenta Paraguay</option>
            <option value="Binance">Binance</option>
            <option value="Efectivo">Efectivo</option>
            <option value="Otro">Otro</option>
          </select></div>
        <button class="btn btn-primary" onclick="addAhorro()">+ Agregar</button>
      </div>
    </div>
    <div class="box">
      <div class="box-title">Metas de ahorro</div>
      <div id="metas-list"></div>
      <details style="margin-top:16px">
        <summary style="cursor:pointer;font-size:13px;font-weight:600;color:var(--accent)">+ Nueva meta</summary>
        <div class="form-row" style="margin-top:12px">
          <div class="f-group"><label>Nombre</label><input id="m-nombre" placeholder="Viaje a Japón"></div>
          <div class="f-group"><label>Objetivo USD</label><input id="m-obj" type="text" inputmode="decimal" data-money placeholder="3,000"></div>
          <div class="f-group" style="flex:0 1 150px;min-width:0"><label>Fecha objetivo</label><input id="m-fecha" type="date"></div>
          <div class="f-group"><label>Tipo</label>
            <select id="m-tipo">
              <option value="cash">Cash (depósitos manuales)</option>
              <option value="btc">Bitcoin (auto desde compras)</option>
            </select></div>
          <div class="f-group" id="m-btc-desde-wrap" style="display:none"><label>BTC desde (fecha)</label><input id="m-btc-desde" type="date" value="2026-07-01"></div>
          <button class="btn btn-primary" onclick="addMeta()">+ Crear meta</button>
        </div>
      </details>
    </div>
  </div>
</div>

<!-- ── AJUSTES ── -->
<div class="tab" id="tab-config">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
    <div class="section-title" style="margin:0">Precios de mercado</div>
    <button class="btn btn-primary" onclick="refreshLive()" id="btn-refresh">↻ Actualizar precios</button>
  </div>
  <div id="live-prices-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:24px"></div>
  <div class="section-title">Otros activos</div>
  <div class="form-card" style="margin-top:0">
    <div class="form-title">Agrega o actualiza precio manual de cualquier activo</div>
    <div id="precios-list"></div>
    <div class="form-row" style="margin-top:12px">
      <div class="f-group"><label>Activo</label><input id="p-activo" placeholder="ETH" style="text-transform:uppercase"></div>
      <div class="f-group"><label>Precio USD</label><input id="p-precio" type="text" inputmode="decimal" data-money placeholder="100,000"></div>
      <button class="btn btn-primary" onclick="updPrecio()">Guardar</button>
    </div>
  </div>
</div>

</main>

<script>
// ── Utils ─────────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const fmt = (n,d=0) => n==null?'—':'$'+Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtCOP = n => Number(n).toLocaleString('es-CO');
const today = () => new Date().toISOString().split('T')[0];

// ── Mes que estás viendo (puedes volver a meses pasados) ──────────────────────
const mesActual = () => new Date().toISOString().slice(0,7);
let MES = mesActual();

function pintarMes(){
  $('mes-picker').value = MES;
  const viejo = MES !== mesActual();
  $('mes-picker').classList.toggle('mes-viejo', viejo);
  $('btn-hoy').style.display = viejo ? 'flex' : 'none';
  // La fecha por defecto al anotar cae dentro del mes que estás viendo
  const qa = $('qa-fecha');
  if (qa) qa.value = viejo ? MES + '-15' : today();
}

function setMes(v){
  if(!v) return;
  MES = v;
  pintarMes();
  loadBudget();
  loadHome();
}

function cambiarMes(delta){
  const [y,m] = MES.split('-').map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  setMes(d.toISOString().slice(0,7));
}
const COLORS = ['#6366f1','#8b5cf6','#06b6d4','#10b981','#f59e0b','#ef4444','#ec4899','#3b82f6'];

// ── Money input formatting ────────────────────────────────────────────────────
const parseMoney = v => parseFloat(String(v).replace(/,/g,'')) || 0;
const fmtInput  = n => n ? Number(n).toLocaleString('en-US',{maximumFractionDigits:2}) : '';

document.addEventListener('input', e => {
  if (!e.target.hasAttribute('data-money')) return;
  const inp = e.target;
  const caret = inp.selectionStart;
  const before = inp.value.length;
  const raw = inp.value.replace(/[^0-9.]/g,'');
  const parts = raw.split('.');
  parts[0] = parts[0].replace(/\\B(?=(\\d{3})+(?!\\d))/g,',');
  inp.value = parts.length > 1 ? parts[0]+'.'+parts.slice(1).join('') : parts[0];
  const shift = inp.value.length - before;
  try { inp.setSelectionRange(caret+shift, caret+shift); } catch(_){}
});

// Set today on date inputs
document.querySelectorAll('input[type=date]').forEach(i=>i.value=today());
pintarMes();
$('mes-label').textContent = new Date().toLocaleDateString('es-CO',{month:'long',year:'numeric'});

// ── Navigation ────────────────────────────────────────────────────────────────
function goto(name,btn){
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b=>b.classList.remove('active'));
  $('tab-'+name).classList.add('active');
  btn.classList.add('active');
  ({home:loadHome,inv:loadInv,budget:loadBudget,ahorros:loadAhorros,config:loadConfig})[name]?.();
}

// ── ONBOARDING ────────────────────────────────────────────────────────────────

// ── HOME ──────────────────────────────────────────────────────────────────────
const charts = {};
function mkChart(id,type,labels,datasets,extra={}){
  if(charts[id]) charts[id].destroy();
  charts[id]=new Chart($(id).getContext('2d'),{type,
    data:{labels,datasets},
    options:{responsive:true,maintainAspectRatio:true,...extra}});
}
const legendOpts={position:'bottom',labels:{color:'#6e6e73',font:{size:11},padding:10,boxWidth:10}};

async function loadHome(){
  const [nw,bud]=await Promise.all([
    fetch('/api/networth').then(r=>r.json()),
    fetch('/api/budget/mes?mes='+MES).then(r=>r.json())
  ]);

  $('nw-val').textContent=fmt(nw.net_worth);
  const inv=nw.inv_total;
  const gan=nw.activos.reduce((s,a)=>s+(a.ganancia||0),0);
  const ganPct=nw.activos[0]?.ganancia_pct;
  const ganCls=gan>=0?'up':'dn'; const ganSign=gan>=0?'+':'';
  $('nw-sub').textContent=`Inversiones ${fmt(inv)} · USD ${fmt(nw.cash_usd)} · COP ${fmtCOP(nw.cash_cop)}`;

  // 4-col stat cards
  $('home-cards').innerHTML=`
    <div class="card">
      <div class="card-lbl">Inversiones</div>
      <div class="card-val">${fmt(inv)}</div>
      ${gan!=null?`<div class="card-change ${ganCls}">${ganSign}${fmt(gan)}</div>`:''}
    </div>
    <div class="card">
      <div class="card-lbl">Efectivo USD</div>
      <div class="card-val" style="color:var(--blue)">${fmt(nw.cash_usd)}</div>
    </div>
    <div class="card">
      <div class="card-lbl">Efectivo COP</div>
      <div class="card-val" style="color:var(--blue);font-size:20px">${fmtCOP(nw.cash_cop)}</div>
      <div class="card-sub">≈ ${fmt(nw.cash_cop/nw.tasa_cop)} USD</div>
    </div>
    <div class="card">
      <div class="card-lbl">Ganancia total</div>
      <div class="card-val" style="color:${gan>=0?'var(--green)':'var(--red)'}">${ganSign}${fmt(gan)}</div>
      <div class="card-sub">${ganSign}${ganPct||0}% sobre costo base</div>
    </div>
  `;

  // helpers for quantity display
  function fmtQty(qty,activo){
    if(!qty||qty<=0) return '';
    const n=(activo||'').toUpperCase();
    if(n==='BTC') return qty.toFixed(8).replace(/\\.?0+$/,'')+'₿';
    return qty.toLocaleString('en-US',{maximumFractionDigits:4})+'u';
  }
  function mkTooltipLabel(items_arr){
    return {callbacks:{label:(ctx)=>{
      const a=items_arr[ctx.dataIndex];
      const qty=a&&a.qty?fmtQty(a.qty,a.activo):'';
      const usd=fmt(ctx.raw);
      return qty?` ${qty} · ${usd}`:` ${usd}`;
    }}};
  }

  // Chart 1: activos — labels con cantidad en BTC
  const activos=nw.activos.filter(a=>a.valor_actual||a.costo_base);
  const activoLabels=activos.map(a=>{
    const qty=a.qty?fmtQty(a.qty,a.activo):'';
    return qty?`${a.activo} (${qty})`:a.activo;
  });
  mkChart('c-activos','doughnut',activoLabels,
    [{data:activos.map(a=>a.valor_actual||a.costo_base),backgroundColor:COLORS,borderWidth:0}],
    {cutout:'62%',plugins:{legend:legendOpts,tooltip:mkTooltipLabel(activos)}});

  // Chart 2: budget spending donut
  const budSpent=bud.filter(b=>b.gastado>0);
  if(budSpent.length){
    mkChart('c-budget','doughnut',
      budSpent.map(b=>b.nombre),
      [{data:budSpent.map(b=>b.gastado),backgroundColor:COLORS,borderWidth:0}],
      {cutout:'62%',plugins:{legend:legendOpts,tooltip:{callbacks:{label:(ctx)=>{
        const mon=budSpent[ctx.dataIndex].moneda||'USD';
        return mon==='COP'?` ${fmtCOP(ctx.raw)} COP`:` ${fmt(ctx.raw)} ${mon}`;
      }}}}});
  } else {
    $('c-budget').parentElement.innerHTML='<div class="box-title">Budget — gastos este mes</div><p style="color:var(--text3);font-size:13px;padding:8px 0">Sin gastos registrados</p>';
  }

  // Chart 3: portfolio by person — labels con BTC qty si aplica
  const tabProm = await Promise.all(['inversiones_personal','inversiones_family','inversiones_papas']
    .map(t=>fetch(`/api/inversiones/${t}/activos`).then(r=>r.json())));
  const portVals = tabProm.map(rows=>rows.reduce((s,r)=>s+(r.valor_actual||r.costo_base||0),0));
  const portBtc  = tabProm.map(rows=>{const b=rows.find(r=>r.activo==='BTC');return b&&b.qty?b.qty:0;});
  const portNames= ['Personal','Family','Papás'];
  const portLabels= portNames.map((n,i)=>portBtc[i]>0?`${n} (${fmtQty(portBtc[i],'BTC')})`:n);
  mkChart('c-portfolio','doughnut',portLabels,
    [{data:portVals,backgroundColor:['#6366f1','#06b6d4','#f59e0b'],borderWidth:0}],
    {cutout:'62%',plugins:{legend:legendOpts,tooltip:{callbacks:{label:(ctx)=>{
      const btc=portBtc[ctx.dataIndex];
      return btc>0?` ${fmtQty(btc,'BTC')} · ${fmt(ctx.raw)} USD`:` ${fmt(ctx.raw)} USD`;
    }}}}});

  // Chart 4: budget bar — limit vs gastado per category
  const budBars=bud.filter(b=>b.limite_mensual>0&&b.tipo==='gasto').slice(0,10);
  // Detect dominant currency across all bars (categories auto-created via tx have null moneda)
  const monFreq={};
  budBars.forEach(b=>{const m=b.moneda||'USD';monFreq[m]=(monFreq[m]||0)+1;});
  const budMon=Object.entries(monFreq).sort((a,b)=>b[1]-a[1])[0]?.[0]||'USD';
  const fmtBudget=v=>budMon==='COP'?fmtCOP(v)+' COP':fmt(v)+' USD';
  mkChart('c-budgetbar','bar',
    budBars.map(b=>b.nombre.length>12?b.nombre.slice(0,11)+'…':b.nombre),
    [
      {label:`Límite (${budMon})`,data:budBars.map(b=>b.limite_mensual),backgroundColor:'#e0e7ff',borderRadius:4},
      {label:`Gastado (${budMon})`,data:budBars.map(b=>b.gastado),backgroundColor:'#6366f1',borderRadius:4},
    ],
    {plugins:{legend:{position:'top',labels:{color:'#6e6e73',font:{size:11}}},
      tooltip:{callbacks:{label:(ctx)=>` ${ctx.dataset.label.split(' ')[0]}: ${fmtBudget(ctx.raw)}`}}},
     scales:{y:{beginAtZero:true,ticks:{color:'#aeaeb2',font:{size:10},callback:v=>fmtBudget(v)}},x:{ticks:{color:'#6e6e73',font:{size:10}}}},
     maintainAspectRatio:false});
  $('c-budgetbar').style.height='200px';

  // Cash breakdown
  const tasa=nw.tasa_cop;
  const ahData=await fetch('/api/ahorros').then(r=>r.json());
  const cashTotalUSD=nw.cash_usd+(nw.cash_cop/tasa);
  // Ajustar cada entrada con el flujo del mes (mismo criterio que la tarjeta Efectivo COP/USD)
  const rawT={};ahData.forEach(a=>rawT[a.moneda]=(rawT[a.moneda]||0)+a.monto);
  const adjHome=a=>{const t=rawT[a.moneda]||0;const nwT=a.moneda==='COP'?nw.cash_cop:nw.cash_usd;return t?a.monto*(nwT/t):a.monto;};
  $('home-cash').innerHTML=`
    <div style="display:flex;flex-direction:column;gap:10px;padding:4px 0">
      ${ahData.map(a=>{const disp=adjHome(a);return `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 14px;background:${a.moneda==='USD'?'var(--blue-bg)':'var(--green-bg)'};border-radius:10px">
          <div>
            <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:2px">${a.cuenta||a.descripcion}</div>
            <div style="font-size:20px;font-weight:700;color:${a.moneda==='USD'?'var(--blue)':'var(--green)'}">${a.moneda==='COP'?fmtCOP(disp)+' COP':fmt(disp)}</div>
            ${a.moneda==='COP'?`<div style="font-size:11px;color:var(--text3)">≈ ${fmt(disp/tasa)} USD</div>`:''}
          </div>
          <div style="font-size:26px">${a.moneda==='USD'?'💵':'💰'}</div>
        </div>`;}).join('')}
      <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:var(--surface2);border-radius:10px;border:1px solid var(--border)">
        <div style="font-size:12px;font-weight:600;color:var(--text2)">Liquidez total (equiv. USD)</div>
        <div style="font-size:18px;font-weight:800;color:var(--text)">${fmt(cashTotalUSD)}</div>
      </div>

      ${nw.deudas>0?`<div style="display:flex;justify-content:space-between;align-items:center;padding:14px;background:var(--red-bg);border-radius:10px">
        <div><div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:3px">Deudas</div>
          <div style="font-size:22px;font-weight:700;color:var(--red)">${fmt(nw.deudas)}</div></div>
        <div style="font-size:28px">📉</div>
      </div>`:''}
    </div>`;
}

// ── INVERSIONES ──────────────────────────────────────────────────────────────
async function loadInv(){
  // Guardar qué detalles están abiertos antes de re-renderizar
  const open=[...document.querySelectorAll('.asset-detail')].filter(d=>d.style.display==='block').map(d=>d.id);

  const [p,f,pa,mts]=await Promise.all([
    fetch('/api/inversiones/inversiones_personal/activos').then(r=>r.json()),
    fetch('/api/inversiones/inversiones_family/activos').then(r=>r.json()),
    fetch('/api/inversiones/inversiones_papas/activos').then(r=>r.json()),
    fetch('/api/metas').then(r=>r.json()),
  ]);
  const sel=$('i-meta');
  sel.innerHTML='<option value="">Sin meta</option>'+mts.map(m=>`<option value="${m.id}">${m.nombre} (${fmt(m.monto_actual)} / ${fmt(m.monto_objetivo)})</option>`).join('');
  renderActivos('inv-personal','inversiones_personal',p);
  renderActivos('inv-family','inversiones_family',f);
  renderActivos('inv-papas','inversiones_papas',pa);

  // Re-abrir los detalles que estaban abiertos
  for(const id of open){
    const det=document.getElementById(id);
    if(!det) continue;
    const withoutPrefix=id.slice(4); // quita "det-"
    const lastDash=withoutPrefix.lastIndexOf('-');
    const tabla=withoutPrefix.slice(0,lastDash);
    const activo=withoutPrefix.slice(lastDash+1);
    det.style.display='block';
    await renderDetail(det,tabla,activo);
  }
}

async function renderActivos(containerId, tabla, activos){
  const el=$( containerId);
  if(!activos.length){el.innerHTML='<p style="color:var(--text3);font-size:13px;padding:8px 0">Sin inversiones registradas</p>';return;}
  el.innerHTML = activos.map(a=>{
    const ganCls = a.ganancia>=0?'badge-green':'badge-red';
    const ganSign = a.ganancia>=0?'+':'';
    const price = a.precio_actual ? fmt(a.precio_actual) : '—';
    const qty = a.qty ? (a.qty < 1 ? a.qty.toFixed(8) : a.qty.toFixed(4)) : '—';
    return `<div class="asset-card" onclick="toggleDetail(this,'${tabla}','${a.activo}')">
      <div class="asset-header">
        <div>
          <div class="asset-name">${a.activo}</div>
          <div class="asset-qty">${qty} unidades · ${a.ops} operaciones</div>
        </div>
        ${a.ganancia!=null?`<span class="badge ${ganCls}">${ganSign}${a.ganancia_pct}%</span>`:'<span class="badge badge-accent">sin precio</span>'}
      </div>
      <div class="asset-numbers">
        <div class="asset-num"><div class="lbl">Costo base</div><div class="val">${fmt(a.costo_base)}</div></div>
        <div class="asset-num"><div class="lbl">Valor actual</div><div class="val">${a.valor_actual!=null?fmt(a.valor_actual):'—'}</div></div>
        <div class="asset-num"><div class="lbl">Ganancia</div><div class="val" style="color:${a.ganancia>=0?'var(--green)':'var(--red)'}">${a.ganancia!=null?ganSign+fmt(a.ganancia):'—'}</div></div>
        <div class="asset-num"><div class="lbl">Precio actual</div><div class="val" style="font-size:13px">${price}</div></div>
      </div>
      <div class="asset-detail" id="det-${tabla}-${a.activo}" onclick="event.stopPropagation()">
        <div style="color:var(--text3);font-size:12px;padding:8px 0">Cargando historial…</div>
      </div>
    </div>`;
  }).join('');
}

async function toggleDetail(card,tabla,activo){
  const det=card.querySelector('.asset-detail');
  if(det.style.display==='block'){det.style.display='none';return;}
  det.style.display='block';
  await renderDetail(det,tabla,activo);
}
async function renderDetail(det,tabla,activo){
  const rows=await fetch(`/api/inversiones/${tabla}`).then(r=>r.json());
  const filtered=rows.filter(r=>r.activo===activo);
  if(!filtered.length){det.innerHTML='<p style="color:var(--text3);font-size:12px">Sin operaciones</p>';return;}
  det.innerHTML=`<table style="width:100%;border-collapse:collapse">
    <tr>
      <th style="text-align:left;font-size:11px;color:var(--text3);padding:4px 10px 12px 0;font-weight:600;letter-spacing:.05em">FECHA</th>
      <th style="text-align:left;font-size:11px;color:var(--text3);padding:4px 10px 12px;font-weight:600;letter-spacing:.05em">TIPO</th>
      <th style="text-align:right;font-size:11px;color:var(--text3);padding:4px 10px 12px;font-weight:600;letter-spacing:.05em">MONTO</th>
      <th style="text-align:right;font-size:11px;color:var(--text3);padding:4px 0 12px 10px;font-weight:600;letter-spacing:.05em">CANTIDAD</th>
      <th style="width:90px"></th>
    </tr>
    ${filtered.map(r=>`
    <tr style="border-top:1px solid var(--border)" id="inv-row-${r.id}">
      <td style="padding:14px 10px 14px 0;font-size:15px;color:var(--text2);white-space:nowrap">${r.fecha}</td>
      <td style="padding:14px 10px"><span class="badge ${r.monto_usd>=0?'badge-green':'badge-red'}" style="font-size:13px;padding:4px 10px">${r.tipo}</span></td>
      <td style="padding:14px 10px;font-weight:600;font-size:15px;text-align:right;white-space:nowrap">${fmt(Math.abs(r.monto_usd))}</td>
      <td style="padding:14px 0 14px 10px;font-size:14px;color:var(--text2);text-align:right;white-space:nowrap">${r.cantidad?Number(r.cantidad).toFixed(6):'—'}</td>
      <td style="padding:4px 0 4px 8px;white-space:nowrap">
        <div style="display:flex;gap:4px;justify-content:flex-end">
          <button onclick="event.stopPropagation();editInvRow(${r.id},'${r.fecha}','${r.tipo}',${r.monto_usd},${r.cantidad||'null'},${r.precio_unitario||'null'},'${tabla}','${activo}')"
            title="Editar"
            style="background:var(--bg);border:1.5px solid var(--border);cursor:pointer;color:var(--text2);font-size:14px;width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center"
            onmouseover="this.style.borderColor='var(--accent)';this.style.color='var(--accent)'"
            onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text2)'">✏️</button>
          <button onclick="event.stopPropagation();delInv(${r.id},'${tabla}','${activo}',this)"
            title="Eliminar"
            style="background:var(--bg);border:1.5px solid var(--border);cursor:pointer;color:var(--text2);font-size:14px;width:36px;height:36px;border-radius:8px;display:flex;align-items:center;justify-content:center"
            onmouseover="this.style.borderColor='#ef4444';this.style.color='#ef4444';this.style.background='#fee2e2'"
            onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text2)';this.style.background='var(--bg)'">✕</button>
        </div>
      </td>
    </tr>
    <tr id="inv-edit-${r.id}" style="display:none;background:var(--bg)">
      <td colspan="5" style="padding:10px 0">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end">
          <div class="f-group" style="margin:0"><label style="font-size:11px">Fecha</label><input id="ie-fecha-${r.id}" type="date" style="font-size:13px;padding:6px 8px"></div>
          <div class="f-group" style="margin:0"><label style="font-size:11px">Tipo</label>
            <select id="ie-tipo-${r.id}" style="font-size:13px;padding:6px 8px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;color:var(--text)">
              <option value="compra">compra</option><option value="venta">venta</option>
            </select>
          </div>
          <div class="f-group" style="margin:0"><label style="font-size:11px">Monto USD</label><input id="ie-monto-${r.id}" type="text" inputmode="decimal" data-money style="font-size:13px;padding:6px 8px;width:100px"></div>
          <div class="f-group" style="margin:0"><label style="font-size:11px">Cantidad</label><input id="ie-qty-${r.id}" type="number" step="any" style="font-size:13px;padding:6px 8px;width:110px"></div>
          <div class="f-group" style="margin:0"><label style="font-size:11px">Precio unitario</label><input id="ie-precio-${r.id}" type="text" inputmode="decimal" data-money style="font-size:13px;padding:6px 8px;width:100px"></div>
          <button onclick="event.stopPropagation();saveInvEdit(${r.id},'${tabla}','${activo}')"
            style="height:34px;padding:0 14px;background:var(--accent);color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600">Guardar</button>
          <button onclick="event.stopPropagation();document.getElementById('inv-edit-${r.id}').style.display='none'"
            style="height:34px;padding:0 12px;background:none;border:1.5px solid var(--border);border-radius:8px;font-size:13px;cursor:pointer;color:var(--text2)">Cancelar</button>
        </div>
      </td>
    </tr>`).join('')}
  </table>`;
}
function editInvRow(id,fecha,tipo,monto,qty,precio,tabla,activo){
  // Cerrar cualquier otro editor abierto
  document.querySelectorAll('[id^="inv-edit-"]').forEach(el=>el.style.display='none');
  const row=document.getElementById('inv-edit-'+id);
  row.style.display='';
  document.getElementById('ie-fecha-'+id).value=fecha;
  document.getElementById('ie-tipo-'+id).value=tipo;
  document.getElementById('ie-monto-'+id).value=fmtInput(Math.abs(monto));
  document.getElementById('ie-qty-'+id).value=qty||'';
  document.getElementById('ie-precio-'+id).value=precio?fmtInput(precio):'';
}
async function saveInvEdit(id,tabla,activo){
  const d={
    id, tabla, activo,
    fecha:document.getElementById('ie-fecha-'+id).value,
    tipo:document.getElementById('ie-tipo-'+id).value,
    monto_usd:parseMoney(document.getElementById('ie-monto-'+id).value),
    cantidad:parseFloat(document.getElementById('ie-qty-'+id).value)||null,
    precio_unitario:parseMoney(document.getElementById('ie-precio-'+id).value)||null,
    notas:document.getElementById('ie-notas-'+id)?.value||'',
  };
  await fetch('/api/inversiones/editar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  const det=document.getElementById('inv-edit-'+id).closest('.asset-detail');
  await renderDetail(det,tabla,activo);
  loadInv();
}
async function delInv(id,tabla,activo,btn){
  if(!confirm('¿Eliminar esta operación?')) return;
  await fetch('/api/inversiones/eliminar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,tabla})});
  const det=btn.closest('.asset-detail');
  await renderDetail(det,tabla,activo);
  loadInv(); // refresca totales
}

async function addInv(){
  const monto=parseMoney($('i-monto').value)||0;
  const d={
    tabla:$('i-tabla').value, tipo:$('i-tipo').value,
    activo:$('i-activo').value.toUpperCase(), fecha:$('i-fecha').value,
    monto_usd:monto||undefined, cantidad:$('i-qty').value||null,
    precio_unitario:parseMoney($('i-precio').value)||null, notas:$('i-notas').value
  };
  if(!d.activo||!d.monto_usd){alert('Activo y monto son requeridos');return;}
  await fetch('/api/inversiones/agregar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  const metaId=$('i-meta').value;
  if(metaId && monto>0) await fetch('/api/metas/sumar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:parseInt(metaId),monto})});
  ['i-monto','i-qty','i-precio','i-notas'].forEach(id=>$(id).value='');
  $('i-meta').value='';
  loadInv();
  loadAhorros();
}

// ── BUDGET ────────────────────────────────────────────────────────────────────
const MONEDA_SYM = {USD:'$',COP:'$',PYG:'₲',EUR:'€',ARS:'$'};
const MONEDA_CLS = {USD:'usd',COP:'cop',PYG:'pyg',EUR:'eur'};
function fmtM(n,mon){
  const s=MONEDA_SYM[mon]||'';
  return s+Number(n).toLocaleString('en-US',{maximumFractionDigits:0});
}

async function limpiarMes(){
  if(!confirm(`Se borrarán TODOS los movimientos de ${MES}.\\n\\nTu dinero en Ahorros y Metas no se toca: arriba seguirás viendo tus saldos reales.\\n\\n¿Empezar este mes de cero?`)) return;
  const r = await fetch('/api/budget/limpiar-mes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mes:MES})}).then(x=>x.json());
  loadBudget(); loadAhorros(); loadHome();
  alert(`Mes ${MES} en cero (${r.borrados} movimientos borrados).`);
}

async function cerrarMes(){
  const r = await fetch('/api/budget/cash?mes='+MES).then(x=>x.json());
  const resumen = r.map(c=>`${c.moneda}: ${fmtM(c.disponible,c.moneda)}`).join('\\n');
  if(!confirm(`Se guardará como saldo de tus cuentas:\\n\\n${resumen}\\n\\nLos movimientos del mes quedan registrados igual. ¿Continuar?`)) return;
  await fetch('/api/budget/cerrar-mes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mes:MES})});
  loadBudget(); loadAhorros(); loadHome();
}

async function loadBudget(){
  const [cats,rec,cash]=await Promise.all([
    fetch('/api/budget/mes?mes='+MES).then(r=>r.json()),
    fetch('/api/budget/recientes?mes='+MES).then(r=>r.json()),
    fetch('/api/budget/cash?mes='+MES).then(r=>r.json()),
  ]);

  // Cash panel — disponible = saldo_ahorros + ingresos_mes - gastos_mes
  $('budget-cash').innerHTML = cash.length ? cash.map(c=>{
    const cls = MONEDA_CLS[c.moneda]||'other';
    const base = (c.saldo_ahorros||0) + (c.ingresos_mes||0);
    const pctGastado = c.gastos_mes && base > 0
      ? Math.min(100, Math.round(c.gastos_mes / base * 100)) : 0;
    const barCls = pctGastado < 50 ? 'bar-green' : pctGastado < 80 ? 'bar-amber' : 'bar-red';
    return `<div class="cash-card ${cls}" ${c.sin_asignar?'style="opacity:.75"':''}>
      <div class="cash-moneda">${c.sin_asignar?'⚠️':'💳'} ${c.nombre} · ${c.moneda}</div>
      <div class="cash-disponible">${fmtM(c.disponible, c.moneda)}</div>
      ${!c.sin_asignar && c.descripcion && c.descripcion!==c.nombre
        ? `<div style="font-size:11px;color:var(--text3);margin:-6px 0 8px">${c.descripcion}</div>` : ''}
      <div class="cash-rows">
        ${c.sin_asignar
          ? `<div class="cash-row"><span class="cash-row-lbl">Gastos antiguos sin cuenta</span></div>`
          : `<div class="cash-row"><span class="cash-row-lbl">Saldo de la cuenta</span><span class="cash-row-val base">${fmtM(c.saldo_ahorros, c.moneda)}</span></div>`}
        ${c.ingresos_mes>0?`<div class="cash-row"><span class="cash-row-lbl">+ Ingresos del mes</span><span class="cash-row-val in">+${fmtM(c.ingresos_mes,c.moneda)}</span></div>`:''}
        ${c.gastos_mes>0?`
        <div class="cash-row"><span class="cash-row-lbl">− Gastado en el mes</span><span class="cash-row-val out">−${fmtM(c.gastos_mes,c.moneda)}</span></div>
        <div class="bar-track" style="margin-top:4px"><div class="bar-fill ${barCls}" style="width:${pctGastado}%"></div></div>
        `:''}
      </div>
    </div>`;
  }).join('') : '';

  // Selector de cuenta del gasto rápido
  const selC=$('qa-cuenta'), prev=selC.value;
  selC.innerHTML='<option value="">¿De qué cuenta?</option>'+
    cash.filter(c=>!c.sin_asignar).map(c=>`<option value="${c.cuenta_id}">${c.nombre}${c.descripcion&&c.descripcion!==c.nombre?' · '+c.descripcion:''} (${c.moneda})</option>`).join('');
  if(prev) selC.value=prev;

  const hayMovimientos = cash.some(c=>(c.ingresos_mes||0)+(c.gastos_mes||0) > 0);
  $('btn-cerrar').style.display = hayMovimientos ? 'block' : 'none';
  $('btn-limpiar').style.display = hayMovimientos ? 'block' : 'none';

  // Populate category select
  const sel=$('qa-cat');
  sel.innerHTML=cats.filter(c=>c.tipo==='gasto').map(c=>`<option value="${c.nombre}">${c.nombre}</option>`).join('');

  // Budget bars
  const gastoCats=cats.filter(c=>c.tipo==='gasto'||(c.tipo==='ahorro'&&c.limite_mensual));
  const sym = c => ({USD:'$',COP:'$',PYG:'₲',EUR:'€',ARS:'$'}[c.moneda||'USD']||'$');
  const fmtC = (n,c) => sym(c)+Number(n).toLocaleString('en-US',{maximumFractionDigits:0});
  $('budget-bars').innerHTML = gastoCats.length ? gastoCats.map(c=>{
    const lim=c.limite_mensual||0;
    const pct=lim>0?Math.min(100,(c.gastado/lim)*100):100;
    const cls=lim>0&&c.gastado>lim?'bar-red':'bar-green';
    return `<div class="budget-item" id="bi-${c.id}">
      <div class="budget-meta">
        <span class="budget-name">${c.nombre}</span>
        <span class="budget-amounts">${fmtC(c.gastado,c)}${lim?' / '+fmtC(lim,c):''} <span style="color:var(--text3);font-size:10px">${c.moneda||'USD'}</span></span>
        <span class="budget-actions">
          <button class="budget-action-btn" title="Editar" onclick="toggleEditCat(${c.id})">✏️</button>
          <button class="budget-action-btn del" title="Eliminar" onclick="deleteCat(${c.id},'${c.nombre.replace(/'/g,"\\'")}')">🗑</button>
        </span>
      </div>
      <div class="bar-track"><div class="bar-fill ${cls}" style="width:${pct}%"></div></div>
      <div class="cat-edit-form" id="cef-${c.id}">
        <div class="form-row">
          <div class="f-group"><label>Nombre</label><input id="ce-nom-${c.id}" value="${c.nombre}" style="min-width:120px"></div>
          <div class="f-group"><label>Límite</label><input id="ce-lim-${c.id}" type="text" inputmode="decimal" data-money value="${lim?fmtInput(lim):''}"></div>
          <div class="f-group"><label>Moneda</label>
            <select id="ce-mon-${c.id}">
              <option value="USD" ${(c.moneda||'USD')==='USD'?'selected':''}>USD $</option>
              <option value="COP" ${c.moneda==='COP'?'selected':''}>COP $</option>
              <option value="PYG" ${c.moneda==='PYG'?'selected':''}>PYG ₲</option>
              <option value="EUR" ${c.moneda==='EUR'?'selected':''}>EUR €</option>
              <option value="ARS" ${c.moneda==='ARS'?'selected':''}>ARS $</option>
            </select></div>
          <div class="f-group"><label>Tipo</label>
            <select id="ce-tipo-${c.id}">
              <option value="gasto" ${c.tipo==='gasto'?'selected':''}>Gasto</option>
              <option value="ahorro" ${c.tipo==='ahorro'?'selected':''}>Ahorro</option>
              <option value="ingreso" ${c.tipo==='ingreso'?'selected':''}>Ingreso</option>
            </select></div>
          <button class="btn btn-primary btn-sm" onclick="saveCat(${c.id})">Guardar</button>
          <button class="btn btn-outline btn-sm" onclick="toggleEditCat(${c.id})">Cancelar</button>
        </div>
      </div>
    </div>`;
  }).join('') : '<p style="color:var(--text3);font-size:13px">Sin categorías. Agrega una abajo.</p>';

  // Recientes
  // Guardamos los movimientos para poder precargar el formulario de edición
  TX_CACHE = {}; for(const r of rec) TX_CACHE[r.id] = r;
  CUENTAS_CACHE = cash.filter(c=>!c.sin_asignar);
  CATS_CACHE = cats;

  $('budget-recientes').innerHTML = rec.length ? `<div style="display:flex;flex-direction:column;gap:2px">`+rec.map(r=>`
    <div id="tx-${r.id}">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid var(--border2);gap:8px">
        <div style="flex:1;min-width:0">
          <div style="font-size:13px;font-weight:500">${r.categoria}</div>
          <div style="font-size:11px;color:var(--text3)">${r.fecha}${r.cuenta_nombre?' · '+r.cuenta_nombre:' · <span style="color:var(--amber)">sin cuenta</span>'}${r.descripcion?' · '+r.descripcion:''}</div>
        </div>
        <div style="font-size:13px;font-weight:600;color:${r.tipo==='ingreso'?'var(--green)':'var(--text)'};white-space:nowrap">
          ${r.tipo==='ingreso'?'+':'−'}${fmt(r.monto)}
        </div>
        <button onclick="editTx(${r.id})" title="Editar"
          style="background:none;border:none;cursor:pointer;color:var(--text3);font-size:13px;padding:2px 4px;border-radius:5px;flex-shrink:0"
          onmouseover="this.style.color='var(--accent)';this.style.background='var(--accent-bg)'"
          onmouseout="this.style.color='var(--text3)';this.style.background='none'">✎</button>
        <button onclick="delTx(${r.id})" title="Eliminar"
          style="background:none;border:none;cursor:pointer;color:var(--text3);font-size:14px;padding:2px 4px;border-radius:5px;flex-shrink:0"
          onmouseover="this.style.color='var(--red)';this.style.background='var(--red-bg)'"
          onmouseout="this.style.color='var(--text3)';this.style.background='none'">✕</button>
      </div>
      <div id="txedit-${r.id}" class="cat-edit-form"></div>
    </div>`).join('')+'</div>' 
  : '<p style="color:var(--text3);font-size:13px">Sin transacciones este mes</p>';
}

let TX_CACHE = {}, CUENTAS_CACHE = [], CATS_CACHE = [];

function editTx(id){
  const box = $('txedit-'+id);
  if(box.style.display === 'block'){ box.style.display='none'; return; }
  const r = TX_CACHE[id]; if(!r) return;
  const cats = CATS_CACHE.filter(c=>c.tipo===r.tipo||c.tipo==='gasto');
  box.innerHTML = `
    <div class="form-row" style="gap:8px">
      <div class="f-group"><label>Fecha</label>
        <input id="tx-f-${id}" type="date" value="${r.fecha}"></div>
      <div class="f-group"><label>Monto</label>
        <input id="tx-m-${id}" type="text" inputmode="decimal" data-money value="${Number(r.monto).toLocaleString('en-US',{maximumFractionDigits:2})}"></div>
      <div class="f-group"><label>Categoría</label>
        <select id="tx-c-${id}">${cats.map(c=>`<option value="${c.nombre}" ${c.nombre===r.categoria?'selected':''}>${c.nombre}</option>`).join('')}</select></div>
      <div class="f-group"><label>¿De qué cuenta?</label>
        <select id="tx-a-${id}"><option value="">Sin asignar</option>${CUENTAS_CACHE.map(c=>`<option value="${c.cuenta_id}" ${c.cuenta_id===r.cuenta_id?'selected':''}>${c.nombre} (${c.moneda})</option>`).join('')}</select></div>
      <div class="f-group" style="flex:1 1 100%"><label>Descripción</label>
        <input id="tx-d-${id}" value="${(r.descripcion||'').replace(/"/g,'&quot;')}" placeholder="opcional"></div>
    </div>
    <div style="display:flex;gap:8px;margin-top:8px">
      <button class="btn btn-primary btn-sm" onclick="guardarTx(${id})">Guardar</button>
      <button class="btn btn-outline btn-sm" onclick="editTx(${id})">Cancelar</button>
    </div>`;
  box.style.display = 'block';
}

async function guardarTx(id){
  const d = {
    id,
    fecha: $('tx-f-'+id).value,
    monto: parseMoney($('tx-m-'+id).value),
    categoria: $('tx-c-'+id).value,
    cuenta_id: $('tx-a-'+id).value || null,
    descripcion: $('tx-d-'+id).value
  };
  if(!d.monto){ alert('El monto no puede ser 0'); return; }
  await fetch('/api/transacciones/editar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  loadBudget(); loadAhorros(); loadHome();
}

async function delTx(id){
  if(!confirm('¿Eliminar esta transacción?')) return;
  await fetch('/api/transacciones/eliminar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  loadBudget();
}

async function addGasto(){
  const d={
    categoria:$('qa-cat').value, monto:parseMoney($('qa-monto').value),
    descripcion:$('qa-desc').value, fecha:$('qa-fecha').value, tipo:'gasto',
    cuenta_id: $('qa-cuenta').value || null
  };
  if(!d.monto){alert('Ingresa un monto');return;}
  if(!d.cuenta_id){alert('Elige de qué cuenta sale el gasto');return;}
  await fetch('/api/transacciones/agregar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  $('qa-monto').value='';$('qa-desc').value='';
  loadBudget();
}

async function addCat(){
  const d={nombre:$('nc-nombre').value,limite_mensual:parseMoney($('nc-limite').value)||null,tipo:$('nc-tipo').value,moneda:$('nc-moneda').value};
  if(!d.nombre){alert('Ingresa el nombre');return;}
  await fetch('/api/categorias/agregar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  $('nc-nombre').value='';$('nc-limite').value='';
  loadBudget();
}

function toggleEditCat(id){
  const f=document.getElementById('cef-'+id);
  f.style.display=f.style.display==='block'?'none':'block';
}

async function saveCat(id){
  const d={
    id, nombre:document.getElementById('ce-nom-'+id).value,
    limite_mensual:parseMoney(document.getElementById('ce-lim-'+id).value)||null,
    moneda:document.getElementById('ce-mon-'+id).value,
    tipo:document.getElementById('ce-tipo-'+id).value,
  };
  await fetch('/api/categorias/actualizar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  loadBudget();
}

async function deleteCat(id,nombre){
  if(!confirm(`¿Eliminar "${nombre}"? También se borrarán sus transacciones.`)) return;
  await fetch('/api/categorias/eliminar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  loadBudget();
}

// ── AHORROS ──────────────────────────────────────────────────────────────────
async function loadAhorros(){
  const [ah,mt,cashData,cfg]=await Promise.all([
    fetch('/api/ahorros').then(r=>r.json()),
    fetch('/api/metas').then(r=>r.json()),
    fetch('/api/budget/cash?mes='+MES).then(r=>r.json()),
    fetch('/api/config').then(r=>r.json())
  ]);
  const tasa=parseFloat(cfg.tasa_cop_usd||4050);

  // Ajuste = movimientos DEL MES que estás viendo. Cada mes es independiente:
  // el saldo de `ahorros` es lo que tienes hoy en las cuentas, así que sumarle
  // meses anteriores restaría dos veces lo que ya está descontado.
  const adj={};
  for(const c of cashData){
    adj[c.moneda] = (c.ingresos_mes||0) - (c.gastos_mes||0);
  }

  // For display: distribute adjustment proportionally among entries of same currency
  const rawTotals={};
  for(const a of ah) rawTotals[a.moneda]=(rawTotals[a.moneda]||0)+a.monto;

  const adjustedMonto = a => {
    const delta = adj[a.moneda] || 0;
    const total = rawTotals[a.moneda] || a.monto;
    // Distribute adjustment proportionally if multiple entries of same currency
    return a.monto + delta * (a.monto / total);
  };

  $('ahorros-list').innerHTML=ah.length?ah.map(a=>{
    const disp=adjustedMonto(a);
    return `<div class="ahorro-item" id="ah-${a.id}">
      <div>
        <div class="ahorro-lbl">${a.cuenta&&a.cuenta!==a.descripcion?a.cuenta:a.descripcion}</div>
        ${a.cuenta&&a.cuenta!==a.descripcion?`<div style="font-size:11px;color:var(--text3)">${a.descripcion}</div>`:''}
      </div>
      <div style="display:flex;align-items:center;gap:6px">
        <div class="ahorro-val">${a.moneda==='COP'?fmtCOP(disp)+' COP':fmt(disp)}</div>
        <button class="btn btn-outline btn-sm" title="Editar" onclick="editAhorro(${a.id},'${(a.descripcion||'').replace(/'/g,"\\'")}','${a.monto}','${a.moneda}','${(a.cuenta||'').replace(/'/g,"\\'")}')">✏️</button>
        <button class="btn btn-outline btn-sm" title="Eliminar" onclick="delAhorro(${a.id})">✕</button>
      </div>
    </div>`;
  }).join('')
  :'<p style="color:var(--text3);font-size:13px">Sin efectivo registrado</p>';

  // Total = sum of adjusted amounts (matches Budget disponible)
  const totalUSD=ah.filter(a=>a.moneda==='USD').reduce((s,a)=>s+adjustedMonto(a),0);
  const totalCOP=ah.filter(a=>a.moneda==='COP').reduce((s,a)=>s+adjustedMonto(a),0);

  $('ahorros-total').innerHTML=`
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-size:12px;font-weight:600;color:var(--text2)">Disponible este mes</span>
      <span style="font-size:16px;font-weight:800;color:var(--blue)">${fmt(totalUSD+(totalCOP/tasa))}</span>
    </div>
    ${totalCOP>0||totalUSD>0?`<div style="font-size:11px;color:var(--text3);text-align:right;margin-top:2px">${fmt(totalUSD)} USD + ${fmtCOP(totalCOP)} COP</div>`:''}`;

  // Preservar qué tablas/meses estaban abiertos antes de re-renderizar
  const openDet=[...document.querySelectorAll('#metas-list details[open]')].map(d=>d.id);
  const openMes=[...document.querySelectorAll('#metas-list tr[data-open="1"]')].map(t=>t.id);
  $('metas-list').innerHTML=mt.length?mt.map(m=>renderMeta(m)).join('')
  :'<p style="color:var(--text3);font-size:13px">Sin metas creadas</p>';
  openDet.forEach(id=>{const d=document.getElementById(id);if(d)d.open=true;});
  openMes.forEach(id=>{const t=document.getElementById(id);if(t){t.style.display='';t.dataset.open='1';}});
  mt.filter(m=>m.plan).forEach(m=>drawMetaChart(m));
}

function mesLbl(m){const M=['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];return M[parseInt(m.slice(5,7),10)-1]+' '+m.slice(2,4);}

function renderMetaPlan(m){
  const p=m.plan; if(!p) return '';
  const req=m.por_mes||p.req_mes_plan;
  const pctMes=req>0?Math.min(100,Math.round(p.ahorrado_mes/req*100)):0;
  const barCls=pctMes>=100?'bar-green':pctMes>=50?'bar-amber':'bar-red';
  const atrasado=p.fin_estimado&&m.fecha_objetivo&&p.fin_estimado>m.fecha_objetivo.slice(0,7);
  window._depData=window._depData||{};
  const isCash=m.tipo==='cash';
  let acum=0;
  const rows=p.meses.map((x,i)=>{
    const futuro=x.mes>p.mes_actual;
    if(!futuro)acum+=x.ahorrado;
    const planAcum=(i+1)*p.req_mes_plan;
    const delta=acum-planAcum;
    const ents=(p.entradas||[]).filter(e=>e.fecha.slice(0,7)===x.mes);
    ents.forEach(e=>{if(e.id)window._depData[e.id]=e;});
    const entHtml=ents.length?ents.map(e=>`
      <div id="depitem-${e.id||('b'+e.fecha+e.monto)}" style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:4px 0;border-bottom:1px dashed var(--border2)">
        <div style="flex:1;min-width:0">
          <span style="color:var(--text3)">${e.fecha.slice(5)}</span>
          ${e.notas?` · <span>${e.notas}</span>`:''}
          ${e.cuenta?` · <span style="color:var(--text3)">${e.cuenta}</span>`:''}
          ${e.origen?` · <span style="color:var(--text3)">${e.origen}</span>`:''}
        </div>
        <b>${fmt(e.monto)}</b>
        ${isCash&&e.id?`<span style="white-space:nowrap">
          <button class="btn btn-outline btn-sm" title="Editar" onclick="editDep(${e.id},${m.id})">✏️</button>
          <button class="btn btn-outline btn-sm" title="Eliminar" onclick="delDep(${e.id})">✕</button></span>`:''}
      </div>`).join('')
      :'<span style="color:var(--text3)">Sin entradas este mes</span>';
    return `<tr style="${futuro?'opacity:.4;':''}${x.mes===p.mes_actual?'font-weight:700;':''}${ents.length?'cursor:pointer;':''}"
        ${ents.length?`onclick="toggleMes('${m.id}','${x.mes}')"`:''}>
      <td style="padding:4px 6px">${ents.length?'▸ ':''}${mesLbl(x.mes)}${ents.length>1?` <span style="font-size:10px;color:var(--accent)">(${ents.length})</span>`:''}</td>
      <td style="padding:4px 6px;text-align:right">${futuro?'—':fmt(x.ahorrado)}</td>
      <td style="padding:4px 6px;text-align:right">${futuro?'—':fmt(acum)}</td>
      <td style="padding:4px 6px;text-align:right;color:var(--text3)">${fmt(planAcum)}</td>
      <td style="padding:4px 6px;text-align:right;color:${futuro?'var(--text3)':delta>=0?'var(--green)':'var(--red)'}">${futuro?'—':(delta>=0?'+':'')+fmt(delta)}</td>
    </tr>
    <tr id="mesdet-${m.id}-${x.mes}" data-open="0" style="display:none">
      <td colspan="5" style="padding:4px 6px 8px 18px;font-size:11px;background:var(--surface2)">${entHtml}</td>
    </tr>`;
  }).join('');
  return `<div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--border2)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
      <span style="font-size:12px;font-weight:600;color:var(--text2)">Este mes (${mesLbl(p.mes_actual)})</span>
      <span style="font-size:12px;font-weight:700">${fmt(p.ahorrado_mes)} de ${fmt(req)} · ${pctMes}%</span>
    </div>
    <div class="bar-track"><div class="bar-fill ${barCls}" style="width:${pctMes}%"></div></div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;font-size:11px;color:var(--text3)">
      <span>Ritmo promedio: <b style="color:var(--text2)">${fmt(p.ritmo)}/mes</b></span>
      ${p.fin_estimado?`<span>A este ritmo terminas: <b style="color:${atrasado?'var(--red)':'var(--green)'}">${mesLbl(p.fin_estimado)}</b>${atrasado?' ⚠️ después de la fecha objetivo':''}</span>`
        :`<span style="color:var(--red)">Sin ritmo aún — registra ahorros para proyectar</span>`}
    </div>
    <canvas id="c-meta-${m.id}" style="margin-top:10px"></canvas>
    <details id="det-plan-${m.id}" style="margin-top:8px">
      <summary style="font-size:12px;color:var(--accent);cursor:pointer;font-weight:600">Ver tabla mes a mes</summary>
      <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:6px">
        <thead><tr style="color:var(--text3);text-align:right;font-size:11px">
          <th style="text-align:left;padding:4px 6px">Mes</th><th style="padding:4px 6px">Ahorrado</th>
          <th style="padding:4px 6px">Acumulado</th><th style="padding:4px 6px">Plan</th><th style="padding:4px 6px">Δ</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </details>
  </div>`;
}

function toggleMes(mid,mes){
  const tr=$(`mesdet-${mid}-${mes}`);
  if(!tr)return;
  const open=tr.dataset.open==='1';
  tr.style.display=open?'none':'';
  tr.dataset.open=open?'0':'1';
}

function editDep(id,metaId){
  event.stopPropagation();
  const e=window._depData[id]; if(!e)return;
  const el=$(`depitem-${id}`);
  el.innerHTML=`<div style="display:flex;gap:4px;flex-wrap:wrap;align-items:center;width:100%">
    <input id="ed-f-${id}" type="date" value="${e.fecha}" style="background:var(--bg);border:1.5px solid var(--border);border-radius:6px;padding:3px 6px;font-size:11px;font-family:inherit">
    <input id="ed-m-${id}" type="text" inputmode="decimal" data-money value="${fmtInput(e.monto)}" style="width:70px;background:var(--bg);border:1.5px solid var(--accent);border-radius:6px;padding:3px 6px;font-size:11px;font-family:inherit">
    <input id="ed-n-${id}" value="${(e.notas||'').replace(/"/g,'&quot;')}" placeholder="Descripción" style="flex:1;min-width:90px;background:var(--bg);border:1.5px solid var(--border);border-radius:6px;padding:3px 6px;font-size:11px;font-family:inherit">
    <input id="ed-c-${id}" value="${(e.cuenta||'').replace(/"/g,'&quot;')}" placeholder="Cuenta" style="width:90px;background:var(--bg);border:1.5px solid var(--border);border-radius:6px;padding:3px 6px;font-size:11px;font-family:inherit">
    <button class="btn btn-primary btn-sm" onclick="saveDep(${id})">✓</button>
    <button class="btn btn-outline btn-sm" onclick="loadAhorros()">✕</button>
  </div>`;
  initMoneyInputs();
}

async function saveDep(id){
  event.stopPropagation();
  const d={id,fecha:$(`ed-f-${id}`).value,monto:parseMoney($(`ed-m-${id}`).value),
    notas:$(`ed-n-${id}`).value,cuenta:$(`ed-c-${id}`).value};
  if(!d.monto){alert('Monto inválido');return;}
  await fetch('/api/metas/depositos/editar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  loadAhorros();
}

async function delDep(id){
  event.stopPropagation();
  if(!confirm('¿Eliminar este ahorro?'))return;
  await fetch('/api/metas/depositos/eliminar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  loadAhorros();
}

function drawMetaChart(m){
  const p=m.plan; if(!p||!$(`c-meta-${m.id}`)) return;
  const labels=p.meses.map(x=>mesLbl(x.mes));
  let idx=p.meses.findIndex(x=>x.mes===p.mes_actual);
  if(idx<0)idx=p.meses.length-1;
  let acum=0;
  const real=p.meses.map((x,i)=>{if(i>idx)return null;acum+=x.ahorrado;return Math.round(acum*100)/100;});
  const plan=p.meses.map((_,i)=>Math.round((i+1)*p.req_mes_plan*100)/100);
  const lastReal=real[idx]||0;
  const proy=p.meses.map((_,i)=>i<idx?null:Math.round((lastReal+p.ritmo*(i-idx))*100)/100);
  mkChart(`c-meta-${m.id}`,'line',labels,[
    {label:'Real',data:real,borderColor:'#6366f1',backgroundColor:'#6366f1',tension:.3,pointRadius:3},
    {label:'Proyección',data:proy,borderColor:'#f59e0b',borderDash:[6,4],pointRadius:0,tension:.3},
    {label:'Plan',data:plan,borderColor:'#c7cbd4',borderDash:[2,3],pointRadius:0},
  ],{plugins:{legend:{position:'top',labels:{color:'#6e6e73',font:{size:10},boxWidth:14}},
      tooltip:{callbacks:{label:c=>` ${c.dataset.label}: ${fmt(c.raw)}`}}},
    scales:{y:{beginAtZero:true,ticks:{color:'#aeaeb2',font:{size:10}}},x:{ticks:{color:'#6e6e73',font:{size:9}}}},
    maintainAspectRatio:false});
  $(`c-meta-${m.id}`).style.height='200px';
}

function renderMeta(m){
  const isBtc=m.tipo==='btc';
  const isCash=m.tipo==='cash';
  const nb=m.nombre.replace(/'/g,"\\'");
  return `<div class="meta-item" id="meta-${m.id}">
    <div class="meta-top">
      <span class="meta-name">${isBtc?'₿ ':''}${m.nombre}</span>
      <div style="display:flex;align-items:center;gap:6px">
        <span class="meta-pct">${m.pct||0}%</span>
        <button class="btn btn-outline btn-sm" title="Editar" onclick="editMeta(${m.id},'${nb}',${m.monto_objetivo},'${m.fecha_objetivo||''}','${m.tipo||'manual'}','${m.btc_desde||''}')">✏️</button>
        <button class="btn btn-outline btn-sm" title="Eliminar" onclick="delMeta(${m.id})">✕</button>
      </div>
    </div>
    <div class="meta-bar"><div class="meta-fill" style="width:${Math.min(m.pct||0,100)}%"></div></div>
    <div class="meta-sub">${fmt(m.monto_actual)} de ${fmt(m.monto_objetivo)} USD${m.por_mes?' · '+fmt(m.por_mes)+'/mes':''} ${m.fecha_objetivo?'· hasta '+m.fecha_objetivo:''}</div>
    ${isBtc?`<div style="font-size:11px;color:var(--text3);margin-top:4px">Auto-calculado · compras BTC personal+family desde ${m.btc_desde||'2026-07-01'}</div>`:''}
    ${renderMetaPlan(m)}
    ${isCash?`<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border2)">
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <input id="dep-m-${m.id}" type="text" inputmode="decimal" data-money placeholder="500"
          style="width:85px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:5px 8px;font-size:12px;font-family:inherit">
        <select id="dep-c-${m.id}" style="background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:5px 8px;font-size:12px;font-family:inherit">
          <option value="GrabFi">GrabFi</option>
          <option value="Listo Global">Listo Global</option>
          <option value="Cuenta Paraguay">Cuenta Paraguay</option>
          <option value="Binance">Binance</option>
          <option value="Efectivo">Efectivo</option>
        </select>
        <input id="dep-n-${m.id}" placeholder="Descripción (opcional)"
          style="flex:1;min-width:110px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:5px 8px;font-size:12px;font-family:inherit">
        <button class="btn btn-primary btn-sm" onclick="addDeposito(${m.id})">+ Depositar</button>
      </div>
      <div id="dep-list-${m.id}" style="margin-top:6px"></div>
    </div>`:''}
  </div>`;
}

function editMeta(id,nombre,objetivo,fecha,tipo,btcDesde){
  const el=$(`meta-${id}`);
  el.innerHTML=`<div style="display:flex;flex-direction:column;gap:8px;padding:4px 0">
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <input id="em-n-${id}" value="${nombre}" placeholder="Nombre"
        style="flex:1;min-width:140px;background:var(--bg);border:1.5px solid var(--accent);border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit">
      <input id="em-o-${id}" type="text" inputmode="decimal" data-money value="${fmtInput(objetivo)}" placeholder="Objetivo USD"
        style="width:120px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit">
      <input id="em-f-${id}" type="date" value="${fecha}"
        style="background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit">
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <select id="em-t-${id}" style="background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:6px 8px;font-size:13px;font-family:inherit">
        <option value="cash" ${tipo==='cash'?'selected':''}>Cash</option>
        <option value="btc" ${tipo==='btc'?'selected':''}>Bitcoin</option>
        <option value="manual" ${tipo==='manual'?'selected':''}>Manual</option>
      </select>
      <input id="em-bd-${id}" type="date" value="${btcDesde}" placeholder="BTC desde"
        style="background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit">
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-primary btn-sm" onclick="saveMeta(${id})">Guardar</button>
      <button class="btn btn-outline btn-sm" onclick="loadAhorros()">Cancelar</button>
    </div>
  </div>`;
  initMoneyInputs();
}

async function saveMeta(id){
  const d={id,nombre:$(`em-n-${id}`).value,monto_objetivo:parseMoney($(`em-o-${id}`).value),
    fecha_objetivo:$(`em-f-${id}`).value||null,tipo:$(`em-t-${id}`).value,btc_desde:$(`em-bd-${id}`).value||null};
  await fetch('/api/metas/editar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  loadAhorros();
}

async function delMeta(id){
  if(!confirm('¿Eliminar esta meta?')) return;
  await fetch('/api/metas/eliminar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  loadAhorros();
}

function editAhorro(id,desc,monto,moneda,cuenta){
  const el=$(`ah-${id}`);
  el.innerHTML=`<div style="display:flex;flex-direction:column;gap:8px;width:100%;padding:4px 0">
    <div style="display:flex;gap:6px;flex-wrap:wrap">
      <input id="ea-d-${id}" value="${desc}" placeholder="Descripción"
        style="flex:1;min-width:120px;background:var(--bg);border:1.5px solid var(--accent);border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit">
      <input id="ea-m-${id}" type="text" inputmode="decimal" data-money value="${fmtInput(parseFloat(monto))}"
        style="width:110px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit">
      <select id="ea-mn-${id}" style="background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:6px 8px;font-size:13px;font-family:inherit">
        <option value="USD" ${moneda==='USD'?'selected':''}>USD</option>
        <option value="COP" ${moneda==='COP'?'selected':''}>COP</option>
        <option value="PYG" ${moneda==='PYG'?'selected':''}>PYG ₲</option>
      </select>
      <select id="ea-c-${id}" style="background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:6px 8px;font-size:13px;font-family:inherit">
        <option value="GrabFi" ${cuenta==='GrabFi'?'selected':''}>GrabFi</option>
        <option value="Listo Global" ${cuenta==='Listo Global'?'selected':''}>Listo Global</option>
        <option value="Cuenta Paraguay" ${cuenta==='Cuenta Paraguay'?'selected':''}>Cuenta Paraguay</option>
        <option value="Binance" ${cuenta==='Binance'?'selected':''}>Binance</option>
        <option value="Efectivo" ${cuenta==='Efectivo'?'selected':''}>Efectivo</option>
        <option value="Otro" ${cuenta==='Otro'?'selected':''}>Otro</option>
      </select>
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-primary btn-sm" onclick="saveAhorro(${id})">Guardar</button>
      <button class="btn btn-outline btn-sm" onclick="loadAhorros()">Cancelar</button>
    </div>
  </div>`;
  initMoneyInputs();
}

async function saveAhorro(id){
  const d={id,descripcion:$(`ea-d-${id}`).value,monto:parseMoney($(`ea-m-${id}`).value),moneda:$(`ea-mn-${id}`).value,cuenta:$(`ea-c-${id}`).value};
  await fetch('/api/ahorros/actualizar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  loadAhorros();
}

async function delAhorro(id){
  if(!confirm('¿Eliminar esta entrada?')) return;
  await fetch('/api/ahorros/eliminar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  loadAhorros();
}

async function addDeposito(metaId){
  const monto=parseMoney($(`dep-m-${metaId}`).value);
  const cuenta=$(`dep-c-${metaId}`).value;
  const notas=$(`dep-n-${metaId}`)?$(`dep-n-${metaId}`).value:'';
  if(!monto){alert('Ingresa un monto');return;}
  await fetch('/api/metas/depositos/agregar',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({meta_id:metaId,monto,cuenta,notas,fecha:new Date().toISOString().split('T')[0]})});
  $(`dep-m-${metaId}`).value='';if($(`dep-n-${metaId}`))$(`dep-n-${metaId}`).value='';
  loadAhorros();
}

async function addAhorro(){
  const d={descripcion:$('a-desc').value,monto:parseMoney($('a-monto').value),moneda:$('a-moneda').value,cuenta:$('a-cuenta').value};
  if(!d.descripcion||!d.monto){alert('Completa todos los campos');return;}
  await fetch('/api/ahorros/agregar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  $('a-desc').value='';$('a-monto').value='';
  loadAhorros();
}

async function addMeta(){
  const tipo=$('m-tipo').value;
  const d={nombre:$('m-nombre').value,monto_objetivo:parseMoney($('m-obj').value),fecha_objetivo:$('m-fecha').value||null,tipo,btc_desde:tipo==='btc'?($('m-btc-desde').value||'2026-07-01'):null};
  if(!d.nombre||!d.monto_objetivo){alert('Nombre y objetivo son requeridos');return;}
  await fetch('/api/metas/agregar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  ['m-nombre','m-obj','m-fecha'].forEach(id=>$(id).value='');
  loadAhorros();
}
// Mostrar/ocultar campo btc_desde según tipo de meta
document.addEventListener('change',e=>{
  if(e.target.id==='m-tipo'){
    const wrap=$('m-btc-desde-wrap');
    if(wrap) wrap.style.display=e.target.value==='btc'?'':'none';
  }
});

// ── CONFIG ────────────────────────────────────────────────────────────────────
const LIVE_CARDS = [
  {key:'BTC',    label:'Bitcoin',   sym:'₿',  color:'#f59e0b', fmt: v=>'$'+Number(v).toLocaleString('en-US',{maximumFractionDigits:0})},
  {key:'MSTR',   label:'MicroStrategy', sym:'📈', color:'#6366f1', fmt: v=>'$'+Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})},
  {key:'COP',    label:'USD → COP', sym:'🇨🇴', color:'#10b981', fmt: v=>Number(v).toLocaleString('es-CO',{maximumFractionDigits:0})+' COP'},
  {key:'PYG',    label:'USD → PYG', sym:'🇵🇾', color:'#3b82f6', fmt: v=>Number(v).toLocaleString('en-US',{maximumFractionDigits:0})+' ₲'},
];

async function loadConfig(){
  const [precios, cfg] = await Promise.all([
    fetch('/api/precios').then(r=>r.json()),
    fetch('/api/config').then(r=>r.json())
  ]);
  const pm = Object.fromEntries(precios.map(x=>[x.activo,x]));
  const copRate = parseFloat(cfg.tasa_cop_usd||0);
  const pygRate = parseFloat(cfg.tasa_pyg_usd||0);

  const liveData = {
    BTC:  pm['BTC']  ? {precio:pm['BTC'].precio_usd,   updated:pm['BTC'].actualizado_en}   : null,
    MSTR: pm['MSTR'] ? {precio:pm['MSTR'].precio_usd,  updated:pm['MSTR'].actualizado_en}  : null,
    COP:  copRate    ? {precio:copRate, updated: cfg.tasa_cop_usd ? 'en DB' : null}         : null,
    PYG:  pygRate    ? {precio:pygRate, updated: cfg.tasa_pyg_usd ? 'en DB' : null}         : null,
  };

  $('live-prices-grid').innerHTML = LIVE_CARDS.map(c=>{
    const d = liveData[c.key];
    return `<div style="background:var(--surface);border-radius:14px;padding:18px;box-shadow:var(--shadow);position:relative;overflow:hidden">
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:${c.color}"></div>
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--text3);margin-bottom:6px">${c.sym} ${c.label}</div>
      <div style="font-size:22px;font-weight:800;letter-spacing:-.5px;color:var(--text);margin-bottom:4px">${d ? c.fmt(d.precio) : '—'}</div>
      <div style="font-size:11px;color:var(--text3)">${d ? 'Actualizado: '+d.updated : 'Sin datos — presiona Actualizar'}</div>
    </div>`;
  }).join('');

  // Otros activos (excluye los que ya están en live cards)
  const liveKeys = new Set(['BTC','MSTR']);
  const otros = precios.filter(x=>!liveKeys.has(x.activo));
  $('precios-list').innerHTML = otros.map(x=>`
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border2)">
      <div>
        <span style="font-weight:700;font-size:15px">${x.activo}</span>
        <span style="font-size:12px;color:var(--text3);margin-left:8px">${x.actualizado_en}</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <input type="text" inputmode="decimal" data-money value="${fmtInput(x.precio_usd)}" id="p-${x.activo}"
          style="width:130px;background:var(--bg);border:1.5px solid var(--border);border-radius:8px;padding:6px 10px;font-size:13px;font-family:inherit;outline:none;color:var(--text)">
        <button class="btn btn-outline btn-sm" onclick="updPrecioById('${x.activo}')">Guardar</button>
      </div>
    </div>`).join('') || '<p style="color:var(--text3);font-size:13px;margin:0">Sin otros activos</p>';
}

async function refreshLive(){
  const btn=$('btn-refresh');
  btn.textContent='Actualizando...'; btn.disabled=true;
  const r = await fetch('/api/precios/live').then(x=>x.json());
  btn.textContent='↻ Actualizar precios'; btn.disabled=false;
  await loadConfig();
  loadHome();
}

async function updPrecioById(activo){
  const precio=document.getElementById('p-'+activo)?.value;
  if(!precio) return;
  await fetch('/api/precios/actualizar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({activo,precio_usd:parseMoney(precio)})});
  loadConfig();
  loadHome();
}

async function updPrecio(){
  const d={activo:$('p-activo').value.toUpperCase(),precio_usd:parseMoney($('p-precio').value)};
  if(!d.activo||!d.precio_usd){alert('Completa activo y precio');return;}
  await fetch('/api/precios/actualizar',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});
  $('p-activo').value='';$('p-precio').value='';
  loadConfig();
}

// ── Init ──────────────────────────────────────────────────────────────────────
pintarMes();
loadHome();
loadBudget();
if("serviceWorker" in navigator){navigator.serviceWorker.register("/sw.js").catch(()=>{});}
</script>
</body>
</html>"""



# ── PWA: manifest + service worker ────────────────────────────────────────────

MANIFEST = {
    "name": "CFO Personal",
    "short_name": "CFO",
    "description": "Tu centro financiero personal: inversiones, budget, ahorros y metas.",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f2f2f7",
    "theme_color": "#f7931a",
    "orientation": "portrait",
    "icons": [
        {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

SW_JS = """const CACHE='cfo-v8';
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/'])))});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()))});
self.addEventListener('fetch',e=>{
  const u=new URL(e.request.url);
  if(e.request.method!=='GET'||u.pathname.startsWith('/api/'))return; // API siempre en red
  e.respondWith(fetch(e.request).then(r=>{const cp=r.clone();caches.open(CACHE).then(c=>c.put(e.request,cp));return r;}).catch(()=>caches.match(e.request)));
});"""


@app.route("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.route("/sw.js")
def service_worker():
    return app.response_class(SW_JS, mimetype="application/javascript")


@app.route("/")
def dashboard(): return render_template_string(DASH)

if __name__ == "__main__":
    print("CFO Personal v2 — http://localhost:3100")
    app.run(host="0.0.0.0", port=3100, debug=False)
