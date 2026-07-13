"""
CFO Personal — Telegram Bot
Bot: @TodyyBot
Corre en Mac Mini con Ollama (qwen2.5:9b o qwen2.5:27b)
"""

import requests
import sqlite3
import json
import time
from pathlib import Path
from datetime import date

TELEGRAM_TOKEN = "8616752475:AAF6d_u7CXoyk-2QuSIpDU9z5jJBXsCaMJg"
CHAT_ID        = 980622410
DB_PATH        = str(Path.home() / "cfo/cfo.db")

# Amadeus (vuelos) — registrarse gratis en developers.amadeus.com
# Free tier: 2,000 requests/mes
AMADEUS_KEY    = "PENDIENTE_AMADEUS_KEY"
AMADEUS_SECRET = "PENDIENTE_AMADEUS_SECRET"

# Modelo Ollama — cambiar a qwen2.5:27b en Mac Mini
OLLAMA_MODEL = "qwen2.5:9b"


# ── Amadeus ─────────────────────────────────────────────────────────────────

def _amadeus_token():
    r = requests.post("https://test.api.amadeus.com/v1/security/oauth2/token", data={
        "grant_type":    "client_credentials",
        "client_id":     AMADEUS_KEY,
        "client_secret": AMADEUS_SECRET,
    }, timeout=10)
    return r.json()["access_token"]


def buscar_vuelos(origen: str, destino: str, fecha: str, adultos: int = 1) -> str:
    if AMADEUS_KEY == "PENDIENTE_AMADEUS_KEY":
        return "Amadeus no configurado aún. Registrarse en developers.amadeus.com y actualizar AMADEUS_KEY en telegram_cfo.py."
    try:
        token = _amadeus_token()
        r = requests.get(
            "https://test.api.amadeus.com/v2/shopping/flight-offers",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "originLocationCode":      origen,
                "destinationLocationCode": destino,
                "departureDate":           fecha,
                "adults":                  adultos,
                "max":                     5,
                "currencyCode":            "USD",
            },
            timeout=15,
        )
        offers = r.json().get("data", [])
        if not offers:
            return "No encontré vuelos para esa ruta y fecha."

        lines = []
        for o in offers[:3]:
            precio    = o["price"]["total"]
            dur       = o["itineraries"][0]["duration"].replace("PT","").replace("H","h ").replace("M","min")
            segmentos = len(o["itineraries"][0]["segments"])
            aerol     = o["itineraries"][0]["segments"][0]["carrierCode"]
            escala    = "directo" if segmentos == 1 else f"{segmentos-1} escala(s)"
            lines.append(f"• {aerol} — {precio} USD — {dur} — {escala}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error buscando vuelos: {e}"


# ── Intent parsing con Ollama ────────────────────────────────────────────────

def parse_intent(message: str) -> dict:
    prompt = f"""Eres un asistente financiero personal. Parsea el mensaje del usuario y retorna JSON.

Mensaje: "{message}"

Retorna exactamente uno de estos JSON (solo el JSON, sin explicación ni markdown):

Registrar inversión (compra):
{{"intent":"inversion","tipo":"compra","tabla":"personal","activo":"BTC","monto_usd":2000,"cantidad":0.02,"precio_unitario":100000,"fecha":"2026-06-26","notas":""}}

Registrar inversión (venta):
{{"intent":"inversion","tipo":"venta","tabla":"personal","activo":"BTC","monto_usd":1000,"cantidad":0.01,"precio_unitario":100000,"fecha":"2026-06-26","notas":""}}

Registrar gasto:
{{"intent":"gasto","categoria":"Alimentación","monto":35,"descripcion":"almuerzo","fecha":"2026-06-26"}}

Registrar ingreso:
{{"intent":"ingreso","categoria":"Salario STG","monto":5000,"fecha":"2026-06-26"}}

Consulta inversiones:
{{"intent":"consulta_inversiones","tabla":"personal|family|papas|todas"}}

Consulta budget:
{{"intent":"consulta_budget"}}

Actualizar precio de activo:
{{"intent":"actualizar_precio","activo":"BTC","precio_usd":105000}}

Buscar vuelo:
{{"intent":"vuelo","origen":"BOG","destino":"TYO","fecha":"2026-10-15"}}

Crear meta de ahorro:
{{"intent":"meta","nombre":"Viaje Tokio","monto_objetivo":2000,"fecha_objetivo":"2026-10-01"}}

No entendido:
{{"intent":"desconocido"}}

Reglas de extracción:
- activo: cualquier ticker o nombre — BTC, MSTR, ETH, S&P, DOGE, acciones, etc. En MAYÚSCULAS.
- monto_usd: el total en dólares invertido/vendido
- cantidad: cuántas unidades (0.02 BTC, 5 acciones MSTR, etc.) — null si no se menciona
- precio_unitario: precio por unidad al momento — null si no se menciona. Si dicen "a 60k" → 60000
- fecha: si dicen "el 20 de noviembre" → "2026-11-20". Si no mencionan fecha → fecha de hoy
- tabla: "family" si mencionan pareja/vamos/nosotros, "papas" si mencionan papás/padres, "personal" por defecto
- Convierte "2000 USD", "dos mil dólares", "2k dólares" → monto_usd: 2000
- Si dicen "BTC a 60k" → precio_unitario: 60000, activo: "BTC"

Hoy es {date.today().isoformat()}."""

    try:
        import ollama
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response["message"]["content"].strip()
        # Limpiar markdown si Ollama lo incluye
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"Ollama error: {e}")
        return {"intent": "desconocido"}


# ── Handlers ─────────────────────────────────────────────────────────────────

def handle(text: str) -> str:
    intent = parse_intent(text)
    conn   = sqlite3.connect(DB_PATH)

    if intent["intent"] == "inversion":
        tabla_map = {"personal": "inversiones_personal", "family": "inversiones_family", "papas": "inversiones_papas"}
        tabla  = tabla_map.get(intent.get("tabla", "personal"), "inversiones_personal")
        activo = str(intent.get("activo", "BTC")).upper()
        tipo   = intent.get("tipo", "compra").lower()
        monto  = float(intent.get("monto_usd", 0))
        if tipo == "venta": monto = -abs(monto)
        qty    = intent.get("cantidad")
        precio = intent.get("precio_unitario")
        fecha  = intent.get("fecha", date.today().isoformat())

        conn.execute(f"""
            INSERT INTO {tabla} (fecha, activo, tipo, cantidad, precio_unitario, monto_usd, notas)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (fecha, activo, tipo, qty or None, precio or None, monto, intent.get("notas", "")))

        # Actualizar precio en tabla precios_mercado si vino precio
        if precio:
            conn.execute("""
                INSERT INTO precios_mercado(activo,precio_usd,actualizado_en) VALUES(?,?,?)
                ON CONFLICT(activo) DO UPDATE SET precio_usd=excluded.precio_usd,actualizado_en=excluded.actualizado_en
            """, (activo, float(precio), fecha))
        conn.commit()

        total_cb = conn.execute(f"SELECT SUM(monto_usd) FROM {tabla} WHERE activo=?", (activo,)).fetchone()[0] or 0
        total_qty = conn.execute(f"SELECT SUM(CASE WHEN monto_usd>0 THEN cantidad ELSE -COALESCE(cantidad,0) END) FROM {tabla} WHERE activo=?", (activo,)).fetchone()[0] or 0
        precio_actual = conn.execute("SELECT precio_usd FROM precios_mercado WHERE activo=?", (activo,)).fetchone()
        conn.close()

        portafolio = tabla.replace("inversiones_", "")
        lines = [f"{'✅' if tipo=='compra' else '🔴'} *{activo}* {tipo} ${abs(float(intent.get('monto_usd',monto))):,.0f} → _{portafolio}_"]
        if qty: lines.append(f"Cantidad: {qty} {activo} {'a' if precio else ''} {'${:,.0f}'.format(float(precio)) if precio else ''}")
        lines.append(f"Costo base {activo}: *${total_cb:,.0f}*")
        if precio_actual and total_qty:
            val = float(total_qty) * precio_actual[0]
            gan = val - total_cb
            lines.append(f"Valor actual: *${val:,.0f}* ({'+'if gan>=0 else ''}${gan:,.0f})")
        return "\n".join(lines)

    elif intent["intent"] == "gasto":
        cat  = intent.get("categoria", "Otros")
        mono = float(intent.get("monto", 0))
        desc = intent.get("descripcion", "")

        if not conn.execute("SELECT id FROM categorias WHERE nombre=?", (cat,)).fetchone():
            conn.execute("INSERT INTO categorias (nombre, tipo) VALUES (?, 'gasto')", (cat,))
        cat_id = conn.execute("SELECT id FROM categorias WHERE nombre=?", (cat,)).fetchone()[0]
        conn.execute("INSERT INTO transacciones (fecha, categoria_id, monto, descripcion, tipo) VALUES (?,?,?,?,'gasto')",
                     (date.today().isoformat(), cat_id, mono, desc))
        conn.commit()

        mes = date.today().strftime("%Y-%m")
        total_cat = conn.execute(
            "SELECT SUM(t.monto) FROM transacciones t JOIN categorias c ON t.categoria_id=c.id WHERE c.nombre=? AND t.fecha LIKE ?",
            (cat, f"{mes}%"),
        ).fetchone()[0] or 0
        conn.close()
        return f"✅ Gasto: *{cat}* ${mono:,.0f} USD\nEste mes en {cat}: *${total_cat:,.0f}*"

    elif intent["intent"] == "ingreso":
        cat  = intent.get("categoria", "Ingreso")
        mono = float(intent.get("monto", 0))

        if not conn.execute("SELECT id FROM categorias WHERE nombre=?", (cat,)).fetchone():
            conn.execute("INSERT INTO categorias (nombre, tipo) VALUES (?, 'ingreso')", (cat,))
        cat_id = conn.execute("SELECT id FROM categorias WHERE nombre=?", (cat,)).fetchone()[0]
        conn.execute("INSERT INTO transacciones (fecha, categoria_id, monto, descripcion, tipo) VALUES (?,?,?,?,'ingreso')",
                     (date.today().isoformat(), cat_id, mono, ""))
        conn.commit()
        conn.close()
        return f"✅ Ingreso: *{cat}* ${mono:,.0f} USD registrado"

    elif intent["intent"] == "actualizar_precio":
        activo = str(intent.get("activo","BTC")).upper()
        precio = float(intent.get("precio_usd", 0))
        if precio:
            conn.execute("""
                INSERT INTO precios_mercado(activo,precio_usd,actualizado_en) VALUES(?,?,?)
                ON CONFLICT(activo) DO UPDATE SET precio_usd=excluded.precio_usd,actualizado_en=excluded.actualizado_en
            """, (activo, precio, date.today().isoformat()))
            conn.commit()
            total_qty = conn.execute(
                f"SELECT SUM(CASE WHEN monto_usd>0 THEN cantidad ELSE -COALESCE(cantidad,0) END) FROM (SELECT monto_usd,cantidad FROM inversiones_personal WHERE activo=? UNION ALL SELECT monto_usd,cantidad FROM inversiones_family WHERE activo=? UNION ALL SELECT monto_usd,cantidad FROM inversiones_papas WHERE activo=?)",
                (activo, activo, activo)
            ).fetchone()[0] or 0
            conn.close()
            val = total_qty * precio
            return f"📈 *{activo}* actualizado a *${precio:,.0f}*\nValor total posición: *${val:,.0f}*"
        conn.close()
        return "Enviame el precio. Ej: 'BTC está a 105000'"

    elif intent["intent"] == "consulta_inversiones":
        tabla_req = intent.get("tabla", "todas")
        if tabla_req == "todas":
            p  = conn.execute("SELECT COALESCE(SUM(monto_usd),0) FROM inversiones_personal").fetchone()[0]
            f  = conn.execute("SELECT COALESCE(SUM(monto_usd),0) FROM inversiones_family").fetchone()[0]
            pa = conn.execute("SELECT COALESCE(SUM(monto_usd),0) FROM inversiones_papas").fetchone()[0]
            conn.close()
            return (f"📊 *Resumen de inversiones*\n\n"
                    f"Personal: *${p:,.0f}*\nFamily: *${f:,.0f}*\nPapás: *${pa:,.0f}*\n\n"
                    f"Patrimonio total (personal+family): *${p+f:,.0f}*")
        else:
            tabla_map = {"personal":"inversiones_personal","family":"inversiones_family","papas":"inversiones_papas"}
            t = tabla_map.get(tabla_req, "inversiones_personal")
            rows = conn.execute(f"SELECT activo, SUM(monto_usd) FROM {t} GROUP BY activo ORDER BY 2 DESC").fetchall()
            conn.close()
            if not rows:
                return "Sin inversiones registradas en ese portafolio."
            lines = [f"• {r[0]}: *${r[1]:,.0f}*" for r in rows]
            total = sum(r[1] for r in rows)
            return f"📊 *Inversiones {tabla_req}*\n\n" + "\n".join(lines) + f"\n\nTotal: *${total:,.0f}*"

    elif intent["intent"] == "consulta_budget":
        mes = date.today().strftime("%Y-%m")
        g = conn.execute("SELECT COALESCE(SUM(monto),0) FROM transacciones WHERE tipo='gasto'   AND fecha LIKE ?", (f"{mes}%",)).fetchone()[0]
        i = conn.execute("SELECT COALESCE(SUM(monto),0) FROM transacciones WHERE tipo='ingreso' AND fecha LIKE ?", (f"{mes}%",)).fetchone()[0]
        top = conn.execute("""
            SELECT c.nombre, SUM(t.monto) FROM transacciones t
            JOIN categorias c ON t.categoria_id=c.id
            WHERE t.tipo='gasto' AND t.fecha LIKE ? GROUP BY c.id ORDER BY 2 DESC LIMIT 5
        """, (f"{mes}%",)).fetchall()
        conn.close()
        lines = "\n".join([f"• {r[0]}: ${r[1]:,.0f}" for r in top]) or "Sin gastos registrados"
        signo = "+" if i - g >= 0 else ""
        return (f"📅 *Budget {mes}*\n\n"
                f"Ingresos: *${i:,.0f}*\nGastos: *${g:,.0f}*\nAhorro: *{signo}${i-g:,.0f}*\n\n"
                f"Top gastos:\n{lines}")

    elif intent["intent"] == "vuelo":
        conn.close()
        res = buscar_vuelos(intent.get("origen","BOG"), intent.get("destino",""), intent.get("fecha",""))
        return f"✈️ *Vuelos {intent.get('origen')} → {intent.get('destino')} ({intent.get('fecha')})*\n\n{res}"

    elif intent["intent"] == "meta":
        conn.execute(
            "INSERT INTO metas (nombre, monto_objetivo, fecha_objetivo, monto_actual) VALUES (?,?,?,0)",
            (intent["nombre"], intent["monto_objetivo"], intent.get("fecha_objetivo")),
        )
        conn.commit()
        conn.close()
        return f"🎯 Meta creada: *{intent['nombre']}* — ${float(intent['monto_objetivo']):,.0f}"

    conn.close()
    return ("No entendí bien. Probá con:\n"
            "• _Compré $500 de BTC_\n"
            "• _Gasté $35 en almuerzo_\n"
            "• _¿Cuánto tengo invertido?_\n"
            "• _¿Cuánto gasté este mes?_\n"
            "• _Vuelos a Tokio en octubre desde BOG_\n"
            "• _Meta: viaje a Japón $2000 para octubre 2027_")


# ── Polling loop ─────────────────────────────────────────────────────────────

BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send(text: str):
    requests.post(f"{BASE}/sendMessage", json={
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": "Markdown",
    }, timeout=10)


def main():
    offset = 0
    print(f"CFO Bot activo — modelo: {OLLAMA_MODEL}")
    while True:
        try:
            r = requests.get(f"{BASE}/getUpdates",
                             params={"offset": offset, "timeout": 30}, timeout=35).json()
            for update in r.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if msg.get("chat", {}).get("id") == CHAT_ID:
                    text = msg.get("text", "").strip()
                    if text:
                        reply = handle(text)
                        send(reply)
        except KeyboardInterrupt:
            print("Bot detenido.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
