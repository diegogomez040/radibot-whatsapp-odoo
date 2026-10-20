# -*- coding: utf-8 -*-
"""
Radibombas WhatsApp Bot v2.0
Bot inteligente para gestión de inventario vía WhatsApp.
"""

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import openai
import xmlrpc.client
import requests
import os
import json
import hmac
import hashlib
import re
import logging
import traceback
import sqlite3
import time
import io
import base64
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

# ──────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

CONVERSATIONS_FILE = DATA_DIR / "conversations.json"
PENDING_FILE = DATA_DIR / "pending.json"
SALES_LOG_FILE = DATA_DIR / "sales_log.json"

DEMAND_DB = DATA_DIR / "demanda_perdida.db"

def init_demand_db():
    conn = sqlite3.connect(DEMAND_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS demanda (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            nombre TEXT NOT NULL,
            categoria TEXT,
            codigo TEXT,
            unidades INTEGER DEFAULT 1
        )
    """)
    # Lista de pedidos al proveedor (2026-10-09): columnas nuevas, la tabla y sus filas viejas se conservan.
    columnas = {r[1] for r in conn.execute("PRAGMA table_info(demanda)")}
    for col, ddl in (("product_id", "INTEGER"), ("motivo", "TEXT"), ("estado", "TEXT DEFAULT 'pendiente'"), ("fecha_estado", "TEXT"), ("clave", "TEXT")):
        if col not in columnas:
            conn.execute(f"ALTER TABLE demanda ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()

init_demand_db()

ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WA_VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN")
# App Secret de la app de Meta: si está definido, se exige la firma X-Hub-Signature-256 en cada webhook.
WA_APP_SECRET = os.getenv("WA_APP_SECRET")
# Cómo se registra la entrega de cada venta en Odoo:
#   "odoo"   = la forma correcta: el bot VALIDA la entrega y es Odoo quien resta el stock (el bot ya no escribe las existencias).
#              Las devoluciones usan la devolución estándar de Odoo, que vuelve a sumar el stock.
#   "simple" = (anterior) el bot resta el stock a mano y cancela la entrega que Odoo crea sola.
# Para volver atrás: cambiar esta línea en .env y reiniciar el bot.
MODO_ENTREGA = (os.getenv("MODO_ENTREGA") or "simple").strip().lower()
# Números de WhatsApp autorizados (con código de país, sin "+"), separados por coma. Vacío = nadie.
ALLOWED_NUMBERS = {n.strip() for n in (os.getenv("ALLOWED_NUMBERS") or "").split(",") if n.strip()}

# Hora: el bot NO depende de la zona horaria del servidor. Todo lo que guarda o compara con Odoo va en UTC
# (sin zona, como Odoo); a Diego se le muestra en hora de Colombia (UTC-5 todo el año, sin horario de verano).
UTC_MENOS_COLOMBIA = timedelta(hours=5)

def ahora_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def medianoche_colombia_en_utc():
    """Inicio del día de hoy EN COLOMBIA, expresado en UTC (para comparar con las fechas de Odoo y de los registros)."""
    hoy_col = (ahora_utc() - UTC_MENOS_COLOMBIA).replace(hour=0, minute=0, second=0, microsecond=0)
    return hoy_col + UTC_MENOS_COLOMBIA

CLIENTE_MOSTRADOR_ID = 7
PENDING_EXPIRY_MINUTES = 10
MAX_HISTORY = 20
MAX_PENDING_USERS = 100
MAX_SALES_LOG = 500

CATEGORIAS = {
    "bombas de agua": 86,
    "mangueras": 81,
    "codos": 87,
    "mangueras corrugadas": 88,
    "mangueras rollos": 89,
    "mangueras para vehiculo": 90,
    "mangueras tramos": 97,
    "radiadores": 82,
    "tapas de tarro de reserva": 93,
    "tapas de radiador": 39,
    "tarros de reserva": 84,
    "termostatos": 94,
    "térmicos": 95,
    "valvulas de calefaccion": 96,
    "ventiladores": 83,
}

STOP_WORDS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "para", "con", "sin", "y", "o", "que", "qué", "cuanto", "cuanta",
    "cuantos", "cuantas", "hay", "tienes", "tiene", "busca", "buscame",
    "a", "al", "es", "se", "vendio", "vendió", "vendi", "vendí",
    "llegaron", "llego", "compramos", "tengo", "tenemos",
}

# ──────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("radibot")

if ALLOWED_NUMBERS:
    log.info(f"Números autorizados: {len(ALLOWED_NUMBERS)}")
else:
    log.error("ALLOWED_NUMBERS está vacío: el bot IGNORARÁ todos los mensajes. Defínelo en .env")

if MODO_ENTREGA not in ("odoo", "simple"):
    log.error(f"MODO_ENTREGA='{MODO_ENTREGA}' no es válido (usa 'odoo' o 'simple'); se usa 'simple'")
    MODO_ENTREGA = "simple"
log.info(f"Modo de entrega: {MODO_ENTREGA}")

if WA_APP_SECRET:
    log.info("Verificación de firma de Meta (X-Hub-Signature-256): ACTIVA")
else:
    log.warning("WA_APP_SECRET no definido: NO se verifica la firma de Meta (cualquiera con la URL podría falsificar mensajes)")

# ──────────────────────────────────────────
# OPENAI
# ──────────────────────────────────────────

client = openai.OpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

# ──────────────────────────────────────────
# PERSISTENCIA EN ARCHIVOS JSON
# ──────────────────────────────────────────

def cargar_json(filepath, default):
    try:
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"Error cargando {filepath}: {e}")
    return default

def guardar_json(filepath, data):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        log.error(f"Error guardando {filepath}: {e}")

def cargar_conversaciones():
    return cargar_json(CONVERSATIONS_FILE, {})

def guardar_conversaciones(conv):
    guardar_json(CONVERSATIONS_FILE, conv)

def cargar_pendientes():
    raw = cargar_json(PENDING_FILE, {})
    ahora = ahora_utc()
    limpio = {}
    for k, v in raw.items():
        try:
            ts = datetime.fromisoformat(v.get("timestamp", ""))
            if ahora - ts < timedelta(minutes=PENDING_EXPIRY_MINUTES):
                limpio[k] = v
        except Exception:
            pass
    return limpio

def guardar_pendientes(p):
    guardar_json(PENDING_FILE, p)

def cargar_ventas_log():
    return cargar_json(SALES_LOG_FILE, [])

def guardar_ventas_log(data):
    guardar_json(SALES_LOG_FILE, data[-MAX_SALES_LOG:])

# ──────────────────────────────────────────
# ACCIONES PENDIENTES
# ──────────────────────────────────────────

def guardar_pendiente(usuario, accion):
    pendientes = cargar_pendientes()
    accion["timestamp"] = ahora_utc().isoformat()
    pendientes[usuario] = accion
    if len(pendientes) > MAX_PENDING_USERS:
        items = sorted(pendientes.items(), key=lambda x: x[1].get("timestamp", ""))
        pendientes = dict(items[-MAX_PENDING_USERS:])
    guardar_pendientes(pendientes)

def obtener_pendiente(usuario):
    return cargar_pendientes().get(usuario)

def limpiar_pendiente(usuario):
    pendientes = cargar_pendientes()
    pendientes.pop(usuario, None)
    guardar_pendientes(pendientes)

# ──────────────────────────────────────────
# LOG DE VENTAS (para deshacer)
# ──────────────────────────────────────────

def registrar_venta_en_log(usuario, product_id, product_name, cantidad, orden_id, stock_anterior):
    ventas = cargar_ventas_log()
    ventas.append({
        "usuario": usuario,
        "timestamp": ahora_utc().isoformat(),
        "product_id": product_id,
        "product_name": product_name,
        "cantidad": cantidad,
        "orden_id": orden_id,
        "stock_anterior": stock_anterior,
        "deshecha": False,
    })
    guardar_ventas_log(ventas)

def ultima_venta_del_usuario(usuario):
    ventas = cargar_ventas_log()
    for v in reversed(ventas):
        if v["usuario"] == usuario and not v.get("deshecha"):
            return v
    return None

def marcar_venta_deshecha(orden_id):
    ventas = cargar_ventas_log()
    for v in ventas:
        if v["orden_id"] == orden_id:
            v["deshecha"] = True
            break
    guardar_ventas_log(ventas)

# ──────────────────────────────────────────
# DEVOLUCIONES (de cualquier venta del registro, total o parcial)
# ──────────────────────────────────────────

def fecha_colombia(iso):
    """'2026-10-06T19:49:12' (hora del servidor, UTC) → 'hoy 14:49' / 'ayer 09:10' / '03/10 16:20' (hora de Colombia, UTC-5)."""
    dt = datetime.fromisoformat(iso) - UTC_MENOS_COLOMBIA
    hoy = (ahora_utc() - UTC_MENOS_COLOMBIA).date()
    dia = "hoy" if dt.date() == hoy else "ayer" if dt.date() == hoy - timedelta(days=1) else dt.strftime("%d/%m")
    return f"{dia} {dt.strftime('%H:%M')}"

def registrar_devolucion_en_log(orden_id, cantidad):
    """Suma lo devuelto a esa venta; si ya se devolvió todo, queda marcada como deshecha."""
    ventas = cargar_ventas_log()
    for v in ventas:
        if v["orden_id"] == orden_id:
            v["devuelto"] = v.get("devuelto", 0) + cantidad
            v.setdefault("devoluciones", []).append({"timestamp": ahora_utc().isoformat(), "cantidad": cantidad})
            if v["devuelto"] >= v["cantidad"]:
                v["deshecha"] = True
            break
    guardar_ventas_log(ventas)

def ventas_para_devolver(query, limite=5):
    """Ventas del registro del bot (con unidades sin devolver) que coinciden con un código o con las palabras del nombre. Las más recientes primero."""
    q = (query or "").strip().lower()
    codigo = codigo_sin_ruido(q) or (q if es_codigo_producto(q) else None)
    palabras = extraer_palabras(q)
    res = []
    for v in reversed(cargar_ventas_log()):
        if v.get("deshecha") or v["cantidad"] - v.get("devuelto", 0) <= 0:
            continue
        nombre = (v.get("product_name") or "").lower()
        if codigo:
            m = re.match(r"\[([^\]]+)\]", nombre)  # "[5490] Manguera…" o "[13070 o 5452] …"
            codigos = [c.strip() for c in re.split(r"\s+o\s+|/", m.group(1))] if m else []
            coincide = q in codigos or codigo in codigos
        else:
            coincide = bool(palabras) and all(p in nombre for p in palabras)
        if coincide:
            res.append(v)
            if len(res) >= limite:
                break
    return res

def nombres_de_ordenes(ids):
    filas = odoo_search_read("sale.order", [["id", "in", list(ids)]], ["name"])
    return {f["id"]: f["name"] for f in filas}

def devolver_en_odoo(orden_id, product_id, cantidad):
    """Devolución en la orden de venta: total → la CANCELA; parcial → baja la cantidad de su línea.
    Comprueba en Odoo que el cambio quedó hecho. Devuelve (ok, mensaje). NO toca el stock."""
    try:
        orden = odoo_search_read("sale.order", [["id", "=", orden_id]], ["name", "state", "order_line"], limit=1)
        if not orden:
            return False, "No encuentro esa orden en Odoo."
        orden = orden[0]
        if orden["state"] != "sale":
            return False, f"La orden {orden['name']} está en estado '{orden['state']}' y no se puede devolver."
        lineas = odoo_search_read("sale.order.line", [["id", "in", orden["order_line"]]], ["product_id", "product_uom_qty"])
        linea = next((l for l in lineas if l.get("product_id") and l["product_id"][0] == product_id), None)
        if not linea:
            return False, f"La orden {orden['name']} no tiene ese producto."
        actual = int(linea["product_uom_qty"])
        if cantidad < 1 or cantidad > actual:
            return False, f"La orden {orden['name']} tiene {actual} unidad(es); no se pueden devolver {cantidad}."
        if cantidad == actual:
            if len(lineas) != 1:
                return False, f"La orden {orden['name']} tiene varios productos; hazlo desde Odoo."
            uid, models = odoo_connect()
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "action_cancel", [[orden_id]])
            despues = odoo_search_read("sale.order", [["id", "=", orden_id]], ["state"], limit=1)
            if not despues or despues[0]["state"] != "cancel":
                return False, f"Odoo no canceló la orden {orden['name']}."
            return True, f"Orden {orden['name']} cancelada."
        nueva = actual - cantidad
        odoo_write("sale.order.line", [linea["id"]], {"product_uom_qty": nueva})
        # Como la entrega original está cancelada, al bajar la cantidad Odoo crea una entrega NUEVA para lo que queda
        # (y reservaría esas unidades): se cancela otra vez.
        cancelar_entregas_de_orden(orden_id)
        leida = odoo_search_read("sale.order.line", [["id", "=", linea["id"]]], ["product_uom_qty"], limit=1)
        if not leida or int(leida[0]["product_uom_qty"]) != nueva:
            return False, f"Odoo no actualizó la cantidad de la orden {orden['name']}."
        return True, f"Orden {orden['name']}: {actual} → {nueva} unidad(es)."
    except Exception as e:
        log.error(f"Error en devolver_en_odoo: {e}")
        return False, f"Odoo respondió con un error: {e}"

def reponer_stock(product_id, cantidad):
    """Suma las unidades al stock ACTUAL (no a un valor viejo, así no pisa ventas ni ingresos posteriores). → (antes, después)."""
    antes = get_stock_actual(product_id)
    ajustar_stock(product_id, antes + cantidad)
    return antes, antes + cantidad

def _falla(msg):
    return {"ok": False, "msg": msg, "stock_repuesto": False, "aviso": None}

def devolucion_en_odoo(orden_id, product_id, cantidad):
    """Devolución de una venta en Odoo → {"ok", "msg", "stock_repuesto", "aviso"}.
    - Si la entrega de esa orden se HIZO en Odoo (ventas hechas con MODO_ENTREGA='odoo'): devolución estándar de Odoo
      (es Odoo quien vuelve a sumar el stock) y luego se ajusta la orden para que el total de ventas quede bien. stock_repuesto=True.
    - Si no (ventas anteriores o modo 'simple'): devolver_en_odoo() y el stock lo suma el bot. stock_repuesto=False."""
    try:
        hechas = odoo_search_read("stock.picking", [["sale_id", "=", orden_id], ["state", "=", "done"], ["picking_type_code", "=", "outgoing"]], ["name"], limit=1)
    except Exception as e:
        log.error(f"Error buscando la entrega de la orden {orden_id}: {e}")
        return _falla(f"Odoo respondió con un error: {e}")
    if not hechas:
        ok, msg = devolver_en_odoo(orden_id, product_id, cantidad)
        return {"ok": ok, "msg": msg, "stock_repuesto": False, "aviso": None}
    return _devolucion_con_entrega_hecha(orden_id, product_id, cantidad, hechas[0])

def _devolucion_con_entrega_hecha(orden_id, product_id, cantidad, entrega):
    try:
        orden = odoo_search_read("sale.order", [["id", "=", orden_id]], ["name", "state", "order_line"], limit=1)
        if not orden:
            return _falla("No encuentro esa orden en Odoo.")
        orden = orden[0]
        nombre = orden["name"]
        if orden["state"] != "sale":
            return _falla(f"La orden {nombre} está en estado '{orden['state']}' y no se puede devolver.")
        lineas = odoo_search_read("sale.order.line", [["id", "in", orden["order_line"]]], ["product_id", "product_uom_qty", "qty_delivered"])
        linea = next((l for l in lineas if l.get("product_id") and l["product_id"][0] == product_id), None)
        if not linea:
            return _falla(f"La orden {nombre} no tiene ese producto.")
        pedida, entregada = int(linea["product_uom_qty"]), int(linea["qty_delivered"])
        if cantidad < 1 or cantidad > min(pedida, entregada):
            return _falla(f"La orden {nombre} tiene {entregada} unidad(es) entregadas; no se pueden devolver {cantidad}.")
        if cantidad == pedida and len(lineas) != 1:
            return _falla(f"La orden {nombre} tiene varios productos; hazlo desde Odoo.")
    except Exception as e:
        log.error(f"Error leyendo la orden {orden_id}: {e}")
        return _falla(f"Odoo respondió con un error: {e}")

    # 1. Devolución estándar de Odoo: crea la recepción de devolución y la valida (Odoo suma el stock)
    stock_antes = get_stock_actual(product_id)
    ret_id = None
    try:
        wiz = odoo_create("stock.return.picking", {"picking_id": entrega["id"]})
        lin_wiz = odoo_search_read("stock.return.picking.line", [["wizard_id", "=", wiz]], ["product_id"])
        mia = next((l for l in lin_wiz if l["product_id"][0] == product_id), None)
        if not mia:
            raise RuntimeError("la entrega no tiene ese producto")
        for l in lin_wiz:  # solo se devuelve el producto de esta venta
            odoo_write("stock.return.picking.line", [l["id"]], {"quantity": float(cantidad) if l["id"] == mia["id"] else 0.0})
        accion = odoo_call("stock.return.picking", "action_create_returns", [wiz])
        ret_id = accion["res_id"]
        for m in odoo_search_read("stock.move", [["picking_id", "=", ret_id]], ["id"]):
            odoo_write("stock.move", [m["id"]], {"to_refund": True})  # que baje lo "entregado" de la orden
        validar_picking(ret_id)
    except Exception as e:
        log.error(f"Devolución de la orden {orden_id} falló: {e}")
        if ret_id:
            try:
                odoo_call("stock.picking", "action_cancel", [ret_id])
            except Exception as e2:
                log.error(f"Tampoco pude cancelar la devolución {ret_id}: {e2}")
        return _falla(f"Odoo no pudo registrar la devolución: {e}")

    aviso = None
    stock_despues = get_stock_actual(product_id)
    if stock_despues != stock_antes + cantidad:
        aviso = f"⚠️ El stock pasó de {stock_antes} a {stock_despues} (se esperaba {stock_antes + cantidad}); revísalo."

    # 2. Ajustar la orden de venta para que el total de ventas refleje la devolución
    try:
        if cantidad == pedida:
            odoo_call("sale.order", "action_cancel", [orden_id])
            est = odoo_search_read("sale.order", [["id", "=", orden_id]], ["state"], limit=1)
            if not est or est[0]["state"] != "cancel":
                raise RuntimeError("Odoo no canceló la orden")
            msg = f"Orden {nombre} cancelada; el stock volvió con una devolución de Odoo."
        else:
            nueva = pedida - cantidad
            odoo_write("sale.order.line", [linea["id"]], {"product_uom_qty": nueva})
            cancelar_entregas_de_orden(orden_id)  # por si Odoo creó una entrega nueva para lo que queda
            leida = odoo_search_read("sale.order.line", [["id", "=", linea["id"]]], ["product_uom_qty"], limit=1)
            if not leida or int(leida[0]["product_uom_qty"]) != nueva:
                raise RuntimeError("Odoo no actualizó la cantidad")
            msg = f"Orden {nombre}: {pedida} → {nueva} unidad(es); devolución registrada en Odoo."
    except Exception as e:
        log.error(f"El stock se devolvió pero no pude ajustar la orden {orden_id}: {e}")
        msg = f"Orden {nombre}: el stock ya se devolvió con una devolución de Odoo."
        extra = f"⚠️ Pero NO pude ajustar la orden {nombre}: {e}. Cambia su cantidad (o cancélala) en Odoo a mano."
        aviso = f"{aviso}\n{extra}" if aviso else extra
    return {"ok": True, "msg": msg, "stock_repuesto": True, "aviso": aviso}

def aplicar_devolucion(orden_id, product_id, cantidad):
    """Hace la devolución completa: Odoo + stock + registro del bot.
    → {"ok", "msg", "antes", "despues", "aviso", "error_stock"} ('antes/despues' = stock del producto)."""
    vigente = variante_vigente(product_id)   # = product_id, salvo ventas anteriores a la reorganización de las corrugadas (variante archivada)
    stock_antes = get_stock_actual(vigente)
    r = devolucion_en_odoo(orden_id, product_id, cantidad)   # la orden y su entrega apuntan al producto VIEJO
    if not r["ok"]:
        return {"ok": False, "msg": r["msg"]}
    if r["stock_repuesto"]:
        try:
            if vigente != product_id:
                trasladar_stock(product_id, vigente, cantidad)   # Odoo devolvió las unidades a la variante archivada: se pasan a la vigente
            antes, despues = stock_antes, get_stock_actual(vigente)
        except Exception as e:
            log.error(f"Error trasladando el stock de la devolución a la variante vigente: {e}")
            registrar_devolucion_en_log(orden_id, cantidad)
            return {"ok": True, "msg": r["msg"], "antes": None, "despues": None, "aviso": None, "error_stock": str(e)}
    else:
        try:
            antes, despues = reponer_stock(vigente, cantidad)
        except Exception as e:
            log.error(f"Error reponiendo stock tras devolución: {e}")
            registrar_devolucion_en_log(orden_id, cantidad)
            return {"ok": True, "msg": r["msg"], "antes": None, "despues": None, "aviso": None, "error_stock": str(e)}
    registrar_devolucion_en_log(orden_id, cantidad)
    return {"ok": True, "msg": r["msg"], "antes": antes, "despues": despues, "aviso": r.get("aviso"), "error_stock": None}

# ──────────────────────────────────────────
# AUDIO (WHISPER)
# ──────────────────────────────────────────

def normalizar_transcripcion(texto):
    """Whisper transcribe mal los códigos dictados: '$54.90', '$13.258', '45.01', '1 3 3 3 7'.
    Se devuelven como dígitos corridos (5490, 13258, 4501, 13337)."""
    # '$13.258' / '$54,90' → sin '$' ni separadores (un precio '$54.900' queda 54900, que es su valor)
    texto = re.sub(r"\$\s?(\d{1,3}(?:[.,]\d{2,3})+)", lambda m: re.sub(r"[.,]", "", m.group(1)), texto)
    # dígitos sueltos con cualquier signo/espacio en medio: '1 3 3 3 7', '5-4-9-0' → 13337, 5490 (4 a 8 dígitos seguidos)
    texto = re.sub(r"\b\d(?:[\s,.;:\-–—_/·'’]+\d){3,7}\b", lambda m: re.sub(r"\D", "", m.group(0)), texto)
    # 'se vendió 45.01' (sin '$') → 4501; solo justo después de un verbo de venta o la palabra código
    texto = re.sub(r"(?i)\b(vendi[óo]|vend[ií]|vendimos|sali[óo]|salieron|c[oó]digo|referencia)\s+(\d{1,3})[.,](\d{2,3})\b", r"\1 \2\3", texto)
    return texto

def transcribir_audio(audio_id):
    try:
        url_info = requests.get(
            f"https://graph.facebook.com/v19.0/{audio_id}",
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
        )
        audio_url = url_info.json().get("url")
        audio_data = requests.get(audio_url, headers={"Authorization": f"Bearer {WA_TOKEN}"})
        with open("/tmp/audio_wa.ogg", "wb") as f:
            f.write(audio_data.content)
        with open("/tmp/audio_wa.ogg", "rb") as f:
            transcripcion = client.audio.transcriptions.create(model="whisper-1", file=f, language="es")
        return normalizar_transcripcion(transcripcion.text)
    except openai.OpenAIError:
        raise   # sin crédito / límite / caída de OpenAI: que lo vea el webhook y avise (no es "no entendí el audio")
    except Exception as e:
        log.error(f"Error transcribiendo audio: {e}")
        return None

def mensaje_error_openai(e):
    """Qué se le dice a quien escribe cuando OpenAI no responde. Antes el error solo quedaba en el log y a WhatsApp no llegaba NADA."""
    detalle = str(e).lower()
    if "insufficient_quota" in detalle or "credit_balance" in detalle or "no credits" in detalle or "billing" in detalle:
        return ("⚠️ Radibot no puede responder ahora: la cuenta de OpenAI se quedó *sin saldo*.\n"
                "Hay que recargarla en platform.openai.com → Billing. Apenas haya saldo vuelve a funcionar solo.\n"
                "(Lo que ya estaba por confirmar sigue guardado.)")
    return "⚠️ El servicio de IA no respondió. Intenta de nuevo en un momento; si sigue, avísale a Diego."

# ──────────────────────────────────────────
# ODOO - CLIENTE
# ──────────────────────────────────────────

def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASSWORD, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
    return uid, models

def odoo_search_read(model, domain, fields, limit=None, order=None):
    uid, models = odoo_connect()
    kwargs = {"fields": fields}
    if limit:
        kwargs["limit"] = limit
    if order:
        kwargs["order"] = order
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, "search_read", [domain], kwargs)

def odoo_write(model, ids, vals):
    uid, models = odoo_connect()
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, "write", [ids, vals])

def odoo_create(model, vals):
    uid, models = odoo_connect()
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, "create", [vals])

def enriquecer_variantes(productos):
    if not productos:
        return productos
    uid, models = odoo_connect()
    for p in productos:
        attr_ids = p.get("product_template_attribute_value_ids", [])
        if attr_ids:
            attrs = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.template.attribute.value", "search_read",
                [[["id", "in", attr_ids]]],
                {"fields": ["attribute_id", "name"]},
            )
            p["_variantes"] = [(a["attribute_id"][1], a["name"]) for a in attrs]
        else:
            p["_variantes"] = []
    return productos

# ──────────────────────────────────────────
# BÚSQUEDA INTELIGENTE
# ──────────────────────────────────────────

FIELDS_PRODUCTO = [
    "display_name", "lst_price", "qty_available", "default_code",
    "product_template_attribute_value_ids", "product_tmpl_id",
]

ORDINALES = {
    1: ["primera", "primero", "la 1", "el 1", "del 1", "opción 1", "opcion 1", "número 1", "numero 1", "1ra", "1ro", "uno"],
    2: ["segunda", "segundo", "la 2", "el 2", "del 2", "opción 2", "opcion 2", "2da", "2do", "dos"],
    3: ["tercera", "tercero", "la 3", "el 3", "del 3", "opción 3", "opcion 3", "tres"],
    4: ["cuarta", "cuarto", "la 4", "el 4", "del 4", "opción 4", "opcion 4", "cuatro"],
    5: ["quinta", "quinto", "la 5", "el 5", "del 5", "opción 5", "opcion 5", "cinco"],
}

_SOLO_OPCION = re.compile(r"(?i)^\s*[¡¿]?\s*(?:(?:la|el|del|opci[oó]n|n[uú]mero|n[°º]|#)\s*)?(?:(\d{1,2})|(primer[oa]|segund[oa]|tercer[oa]|cuart[oa]|quint[oa]|uno|dos|tres|cuatro|cinco))\s*[.!,]*\s*$")
_NUMERO_PALABRA = {"primero": 1, "primera": 1, "uno": 1, "segundo": 2, "segunda": 2, "dos": 2, "tercero": 3, "tercera": 3, "tres": 3,
                   "cuarto": 4, "cuarta": 4, "cuatro": 4, "quinto": 5, "quinta": 5, "cinco": 5}

def opcion_suelta(texto):
    """El mensaje es SOLO una opción («3», «el 3», «opción 2», «el segundo», «dos»; un número de 1 o 2 dígitos) → ese número; si dice algo más, None. (Las listas son de 5 como máximo: quien decide si el número vale es quien la usa.)"""
    m = _SOLO_OPCION.match(texto or "")
    if not m:
        return None
    return int(m.group(1)) if m.group(1) else _NUMERO_PALABRA.get(m.group(2).lower())

def mensaje_opcion_sin_lista(usuario, opcion):
    """El modelo pasó `opcion_lista` pero no hay (o no sirve) la lista: antes salía «No encontré: **»."""
    pend = obtener_pendiente(usuario)
    opciones = (pend or {}).get("opciones") or []
    if opciones and pend.get("tipo") in ("lista_productos", "seleccion_venta", "seleccion_actualizacion"):
        return f"Opción inválida (1 a {len(opciones)})."
    return "No tengo una lista abierta (se borra al cancelar y a los 10 minutos). Dime el código o el nombre del producto."

def detectar_ordinal(texto):
    """Detecta si el texto contiene referencia a una opción numerada."""
    texto_lower = texto.lower()
    # Si el mensaje menciona múltiples productos (tiene "y"), no detectar ordinales numéricos
    # para evitar confundir "dos mangueras X y dos mangueras Y" con "opción 2"
    tiene_multiplos = bool(re.search(r'\b(y|e)\b', texto_lower)) and re.search(r'\d', texto_lower)
    
    for num, palabras in ORDINALES.items():
        for p in palabras:
            # (?!\d): "la 5" no debe detectarse dentro de "la 5490" (un código, no la opción 5)
            if re.search(re.escape(p) + r"(?!\d)", texto_lower):
                # Evitar falsos positivos con "uno", "dos", "tres" en mensajes con múltiples productos
                if p in ("uno", "dos", "tres", "cuatro", "cinco") and tiene_multiplos:
                    continue
                return num
    return None

_CODIGO_TRAS_ARTICULO = re.compile(r"(?i)\b(?:la|el|las|los|del|al|vendi[óo]|vend[ií]|sali[óo]|c[oó]digo|referencia|devolvi[óo]|devolvieron|devoluci[oó]n(?:\s+de)?|(?:fotos?|im[aá]genes|im[aá]gen|imagenes|imagen)(?:\s+(?:de|del|la|el))?)\s+(\d{3,8})\b")

# Pedidos de foto: "dame la imagen de 5490", "quiero ver la foto de las mangueras superiores del aveo"
_PALABRAS_IMAGEN = re.compile(r"(?i)\b(?:fotos?|fotic[oa]s?|fotograf[ií]as?|im[aá]genes|im[aá]gen|imagenes|imagen)\b")
_VERBOS_PEDIR = re.compile(r"(?i)\b(?:dame|dime|mu[eé]strame|muestra|quiero|ver|env[ií]ame|m[aá]ndame|p[aá]same|me|puedes|la|el|las|los|de|del|un|una)\b")

def pide_imagen(texto):
    return bool(_PALABRAS_IMAGEN.search(texto or ""))

def quitar_palabras_imagen(query):
    """'foto superior aveo' → 'superior aveo' (ningún producto lleva 'foto' en el nombre)."""
    q = _VERBOS_PEDIR.sub(" ", _PALABRAS_IMAGEN.sub(" ", query or ""))
    return re.sub(r"\s+", " ", q).strip()

_RELLENO_PEDIDO = {"foto", "fotos", "imagen", "imagenes", "dame", "dime", "dejame", "muestrame", "muestra", "quiero", "ver", "enviame", "mandame",
                   "pasame", "puedes", "favor", "por", "mira", "ensename", "necesito", "como", "queda", "quedo", "esa", "ese", "este", "esta"}

def _palabras_sin_tilde(texto):
    t = unicodedata.normalize("NFD", (texto or "").lower())
    return re.findall(r"[a-z0-9]+", "".join(c for c in t if unicodedata.category(c) != "Mn"))

def filtrar_por_distintivos(texto, productos):
    """Pedido de foto con varios resultados parecidos: 'foto del térmico USA' encuentra el USA y el CHINA (mismo código 4HK1). Las palabras
    del mensaje que están en el nombre de unos resultados y NO en todos ('usa') dejan solo los que las tienen. Antes el modelo, anclado a
    la búsqueda anterior ('4HK1'), soltaba la palabra y se mandaban las dos fotos (6-8 de cada 8 pruebas con el chat real). Si ninguno
    cumple todas las palabras distintivas (p. ej. 'usa china'), se dejan todos."""
    if len(productos) < 2:
        return productos
    sin_tilde_stop = {w for x in STOP_WORDS for w in _palabras_sin_tilde(x)} | _RELLENO_PEDIDO
    palabras = {w for w in _palabras_sin_tilde(texto) if len(w) >= 3 and w not in sin_tilde_stop}
    nombres = [set(_palabras_sin_tilde(p["display_name"])) for p in productos]
    distintivas = [w for w in palabras if 0 < sum(w in n for n in nombres) < len(productos)]
    if not distintivas:
        return productos
    elegidos = [p for p, n in zip(productos, nombres) if all(w in n for w in distintivas)]
    return elegidos or productos

def query_es_otro_codigo(query, sel):
    """True si el usuario dijo un código distinto al del producto de la lista anterior:
    en ese caso no se debe asumir 'ese mismo' (atajo de lista con 1 solo producto)."""
    q = (query or "").strip().lower()
    if not (es_codigo_producto(q) or codigo_sin_ruido(q)):
        return False
    return q not in (sel.get("codigo") or "").lower()

_PARAM_CONSULTA = {"vender": "producto_query", "actualizar": "producto_query", "devolver": "producto_query", "buscar": "query"}

_NUMERO_SUELTO = re.compile(r"^\s*\$?\s*(\d{1,4}(?:[.,\s]\d{1,4}){1,3})\s*[.,]?\s*$")

def _existe_codigo(codigo):
    try:
        return bool(odoo_search_read("product.product", [["default_code", "=", codigo]], ["id"], limit=1))
    except Exception as e:
        log.warning(f"No pude comprobar el código {codigo}: {e}")
        return False

def atajo_codigo_suelto(usuario, texto):
    """«19.95», «49.30», «13.258» (SOLO eso, con punto o coma): el 13/10 el modelo lo tomó por un PRECIO y armó «cambiar el precio de la 882605 a $1,995» cuando
    Diego quería el producto de código 1995. Si los dígitos juntos son el código de un producto que existe, se busca ese producto, sin pasar por el modelo.
    Si no existe, sigue el camino normal (puede ser una cantidad o un precio de verdad)."""
    m = _NUMERO_SUELTO.match(texto or "")
    if not m or not re.search(r"[.,]", texto):
        return None
    codigo = re.sub(r"\D", "", m.group(1))
    if not 3 <= len(codigo) <= 8 or not _existe_codigo(codigo):
        return None
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") in _TIPOS_EJECUTABLES:
        return None   # con una venta/actualización por confirmar no se pisa el pendiente: el modelo decide como siempre
    return handler_buscar(usuario, {"query": codigo})

_FRAGMENTOS_CODIGO = re.compile(r"(?<![\w-])\d{1,4}(?:(?:\s*[,.]\s*|\s+y\s+|\s+)\d{1,4}){1,3}(?![\w-])")

def unir_codigo_partido(texto):
    """«se vendió la 88 2606», «se vendió la 88, 20 y 19» → 882606, 882019: un código dicho o escrito en pedazos. Se pegan los pedazos SOLO si el resultado (5 a 8
    dígitos) es el código de un producto que existe, ningún pedazo es por sí solo un código y alguno es corto (así «5490 5492 5491» o «2 y 3» no se tocan)."""
    def unir(m):
        pedazos = re.findall(r"\d+", m.group(0))
        junto = "".join(pedazos)
        if not 5 <= len(junto) <= 8 or all(len(p) > 3 for p in pedazos):
            return m.group(0)
        if any(len(p) >= 4 and _existe_codigo(p) for p in pedazos) or not _existe_codigo(junto):
            return m.group(0)
        return junto
    return _FRAGMENTOS_CODIGO.sub(unir, texto or "")

_ESE_MISMO = re.compile(r"(?i)\b(?:es[ae]|la|el|lo)\s+mism[oa]\b")

def codigo_unico_en_ultima_respuesta(historial):
    """Código `[1237]` del producto del último mensaje del bot, si ese mensaje muestra UN solo producto (si hay varios, None)."""
    for m in reversed(historial or []):
        if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
            codigos = set(re.findall(r"\[([^\]\s][^\]]*)\]", m["content"]))
            return next(iter(codigos)) if len(codigos) == 1 else None
    return None

def corregir_ese_mismo(nombre, args, texto, codigo_previo):
    """'actualiza el costo de ESA MISMA' / 'vende ese mismo': el modelo a veces busca por el nombre (que puede tener 13 variantes
    → '¿cuál actualizo?') en vez de usar el código que el bot acaba de mostrar. Con un único producto en el mensaje anterior, el
    código manda. True si corrigió algo."""
    if nombre not in ("actualizar", "vender") or not codigo_previo or not _ESE_MISMO.search(texto or ""):
        return False
    if re.search(r"\b\d{3,8}\b", texto or "") or args.get("opcion_lista"):
        return False   # el usuario dio un código o una opción de lista: eso manda
    if (args.get("producto_query") or "").strip().lower() == codigo_previo.lower():
        return False
    args["producto_query"] = codigo_previo
    args.pop("categoria", None); args.pop("atributos", None)
    return True

_DICE_COSTO = re.compile(r"(?i)\bcost[oó]s?\b")
_DICE_PRECIO = re.compile(r"(?i)\bprecios?\b")

def costo_mandado_como_precio(nombre, args, texto):
    """'Actualiza el COSTO de esa misma a 78075': antes `actualizar` no tenía campo de costo y el modelo lo mandó como `nuevo_precio`
    (habría cambiado el PRECIO DE VENTA de $4,000 a $78,075; Diego lo vio en la vista previa y canceló). Si el mensaje habla de costo
    y no de precio ('precio de compra' cuenta como costo), el valor pasa a `nuevo_costo`. True si corrigió algo."""
    if nombre != "actualizar" or args.get("nuevo_precio") is None or args.get("nuevo_costo") is not None:
        return False
    t = re.sub(r"(?i)\bprecio de compra\b", "costo", texto or "")
    # «…a 78075 pesos, NO el precio» / «no cambies el precio»: nombrar el precio para descartarlo no es hablar del precio
    t = re.sub(r"(?i)\b(?:no|ni|sin)\s+(?:(?:es|el|la|lo|del|de|tu|su|cambies|cambiar|cambio|tocar|toques|modifiques|modificar|actualices|actualizar|mover|muevas)\s+){0,3}precio(?:\s+de\s+venta)?\b", "", t)
    if _DICE_COSTO.search(t) and not _DICE_PRECIO.search(t):
        args["nuevo_costo"] = args.pop("nuevo_precio")
        return True
    return False

def corregir_por_codigo(nombre, args, texto, n_llamadas=1):
    """'Se vendió la 5490': si el usuario dijo un código explícito, el modelo no puede cambiarlo:
    ni tomarlo por opcion_lista (las opciones son 1 a 5) ni sustituirlo por otro producto del historial.
    Solo se corrige si existe un producto con ese código (así 'el 2008' como año no se confunde).
    Con varios productos en el mismo mensaje (varias llamadas o varios números) no se toca nada."""
    param = _PARAM_CONSULTA.get(nombre)
    if not param or n_llamadas > 1:
        return False
    if len(set(re.findall(r"\b\d{3,8}\b", texto or ""))) > 1:
        return False
    codigos = set(_CODIGO_TRAS_ARTICULO.findall(texto or ""))
    if len(codigos) != 1:
        return False
    codigo = codigos.pop()
    if str(args.get(param) or "").strip() == codigo and not args.get("opcion_lista"):
        return False  # ya está bien
    if not odoo_search_read("product.product", [["default_code", "ilike", codigo]], ["id"], limit=1):
        return False
    args.pop("opcion_lista", None)
    args[param] = codigo
    return True

_CODIGO_CON_GUION = re.compile(r"(?<![\w-])(?:\d{1,2}-\d{2,8}(?:-\d{1,4})*|[A-Za-z]{1,3}-\d{1,5})(?![\w-])")

def _distancia_edicion(a, b):
    """Letras que hay que cambiar/poner/quitar para pasar de a a b (Levenshtein; los códigos son cortos)."""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]

def fijar_codigos_escritos(nombre, args, texto):
    """El modelo a veces CAMBIA un dígito de un código con guiones que el usuario escribió: el 14/10 «térmico 8-97129-735-0» (el del Elf, stock 1) llegó como
    «8-97129-738-0» (el del NPR, $430,000: otro producto REAL que había visto en una lista); 3 de 3 pruebas. `corregir_por_codigo` solo cubre códigos de puros
    dígitos. Si la búsqueda del modelo es un código con esa forma y NO es uno de los que el usuario escribió, se pone el escrito más parecido (hasta 3 letras
    de diferencia; si escribió uno solo, ese). Un código que no existe se dice «no encontré»: jamás se sustituye por otro producto. True si corrigió algo."""
    param = _PARAM_CONSULTA.get(nombre)
    q = str(args.get(param) or "").strip() if param else ""
    escritos = [m.group(0) for m in _CODIGO_CON_GUION.finditer(texto or "")]
    if not q or not escritos or not _CODIGO_CON_GUION.fullmatch(q):
        return False
    if q.lower() in {e.lower() for e in escritos}:
        return False
    unicos = {e.lower(): e for e in escritos}
    if len(unicos) == 1:
        mejor = next(iter(unicos.values()))   # escribió UN solo código: es ese, por distinto que venga el del modelo
    else:
        mejor = min(escritos, key=lambda e: _distancia_edicion(q.lower(), e.lower()))
        if _distancia_edicion(q.lower(), mejor.lower()) > 3:
            return False
    args[param] = mejor
    args.pop("opcion_lista", None)
    return True

def detectar_ordinales(texto):
    """Todas las opciones (1 a 5) que menciona el texto: 'foto de la 1 y la 3' → [1, 3].
    No cuenta números escritos con letras sueltos ('dos') para no confundirlos con cantidades."""
    t = (texto or "").lower()
    encontradas = set()
    for num, palabras in ORDINALES.items():
        for p in palabras:
            if p in ("uno", "dos", "tres", "cuatro", "cinco"):
                continue
            if re.search(r"(?<!\w)" + re.escape(p) + r"(?!\d)", t):
                encontradas.add(num)
    # "la 1 y 3", "las 1, 2 y 3"
    for grupo in re.findall(r"(?<!\w)(?:la|las|el|los|del|opci[oó]n(?:es)?)\s+(\d(?:\s*(?:,|y|e)\s*\d)+)(?!\d)", t):
        encontradas.update(int(d) for d in re.findall(r"\d", grupo))
    # cualquier número de 1 o 2 dígitos tras "la/el/opción" (para poder avisar "opción inválida" con 'la 9')
    encontradas.update(int(n) for n in re.findall(r"(?<!\w)(?:la|el|del|opci[oó]n|n[uú]mero)\s+(\d{1,2})(?!\d)", t))
    return sorted(encontradas)

def atajo_foto_opcion(usuario, texto):
    """'necesito ver la foto de la 2' tras una lista → fotos de esas opciones (sin pasar por el modelo).
    La lista pendiente se conserva para poder seguir diciendo 'la 3', 'se vendió la 2', etc."""
    if not pide_imagen(texto) or _CODIGO_TRAS_ARTICULO.search(texto):
        return None
    nums = detectar_ordinales(texto)
    if not nums:
        return None
    pend = obtener_pendiente(usuario)
    if not pend or pend.get("tipo") not in ("lista_productos", "seleccion_venta", "seleccion_actualizacion"):
        # Sin lista activa: si el mensaje es SOLO "foto de la 2" (sin más palabras de producto), avisar en vez de dejar que el modelo invente una búsqueda
        resto = re.sub(r"(?i)\b(?:primer[oa]|segund[oa]|tercer[oa]|cuart[oa]|quint[oa]|opci[oó]n|opciones|n[uú]mero|necesito|por favor|gracias|y|e)\b|[\d,.¿?¡!]+", " ", quitar_palabras_imagen(texto))
        if not resto.strip():
            return "No tengo una lista activa (expira a los 10 minutos). Dime el código o el producto y te muestro la foto."
        return None
    opciones = pend.get("opciones") or []
    if any(n < 1 or n > len(opciones) for n in nums):
        return f"Opción inválida (1 a {len(opciones)})."
    ids = [opciones[n - 1]["id"] for n in nums]
    por_id = {p["id"]: p for p in odoo_search_read("product.product", [["id", "in", ids]], FIELDS_PRODUCTO)}
    productos = [por_id[i] for i in ids if i in por_id]
    if not productos:
        return "❌ No pude cargar esos productos."
    return preparar_envio_imagenes(usuario, productos, "opción " + ", ".join(str(n) for n in nums), False)

_VERBO_VENTA = re.compile(r"(?i)\b(?:vend[ií]|vendi[óo]|vendimos|vendieron|sali[óo]|salieron)\b")
_ARTICULO_INICIAL = re.compile(r"(?i)^(?:la|el|las|los|del|al|de)\s+")

def partir_codigos_venta(texto):
    """'se vendió la 54.90, 54.92 y 2 del 5491' → [('54.90', 1), ('54.92', 1), ('5491', 2)].
    Devuelve None si el mensaje no es una venta que sea SOLO una lista de 2 o más códigos
    (cualquier otra cosa la maneja el modelo)."""
    if not _VERBO_VENTA.search(texto or ""):
        return None
    t = re.sub(r"(?i)\bse\b", " ", _VERBO_VENTA.sub(" ", texto))
    t = re.sub(r"[.!?¡¿]+\s*$", "", t.strip())
    partes = re.split(r"\s*;\s*|\s*,\s+|\s*,(?=\d)|\s+y\s+|\s+e\s+|\s*\+\s*", t)
    items = []
    for parte in partes:
        parte = parte.strip().strip(".!?¡¿ ")
        if not parte:
            continue
        # "5490 5492 5491": códigos de 4+ dígitos separados solo por espacios
        subpartes = re.findall(r"\d{4,8}", parte) if re.fullmatch(r"\d{4,8}(?:\s+\d{4,8})+", parte) else [parte]
        for s in subpartes:
            s = _ARTICULO_INICIAL.sub("", s.lower())
            cant = 1
            m = (re.fullmatch(r"(\d{1,2})\s*(?:x|unidades?|unds?|uds?)?\s*del?\s+(?:la\s+|el\s+|los\s+|las\s+)?(.+)", s)  # "2 del 5490"
                 or re.fullmatch(r"(\d{1,2})\s*x\s*(.+)", s))                                                                # "2x5490"
            if m:
                cant, s = int(m.group(1)), m.group(2).strip()
            else:
                m = re.fullmatch(r"(.+?)\s*x\s*(\d{1,2})", s)                                                                # "5490 x2"
                if m:
                    s, cant = m.group(1).strip(), int(m.group(2))
            s = _ARTICULO_INICIAL.sub("", s)
            es_cod = codigo_sin_ruido(s) or (es_codigo_producto(s) and (re.search(r"[a-z]", s) or len(re.sub(r"\D", "", s)) >= 3))
            if not es_cod or cant < 1:
                return None
            items.append((s, cant))
    return items if len(items) >= 2 else None

def buscar_por_codigo(texto_codigo, limit=4):
    """Solo por default_code: tal cual primero y, si no aparece, solo dígitos. → (productos, código usado)."""
    candidatos = [c for c in dict.fromkeys([texto_codigo.strip(), codigo_sin_ruido(texto_codigo)]) if c]
    for c in candidatos:
        prods = odoo_search_read("product.product", [["default_code", "ilike", c]], FIELDS_PRODUCTO, limit=limit)
        if prods:
            return prods, c
    return [], None

def _coincide_exacto(p, c):
    codigo = (p.get("default_code") or "").lower().strip()
    return c.lower() == codigo or c.lower() in [x.strip() for x in re.split(r"\s+o\s+|/", codigo)]

def atajo_venta_multiple(usuario, texto):
    """'se vendió la 5490, 5492, 5491' → una sola venta pendiente con los 3 productos (sin pasar por el modelo).
    Cada código se busca solo por default_code; los que no aparecen o son ambiguos se avisan y no se agregan."""
    items = partir_codigos_venta(texto)
    if not items:
        return None
    nuevas, problemas = {}, []
    for codigo, cant in items:
        prods, usado = buscar_por_codigo(codigo)
        if not prods:
            problemas.append(f"• {codigo}: no encontré ese código")
            continue
        exactos = [p for p in prods if _coincide_exacto(p, usado)]
        elegidos = exactos or prods
        if len(elegidos) != 1:
            nombres = "; ".join(p["display_name"][:45] for p in elegidos[:3])
            problemas.append(f"• {codigo}: hay varios productos con ese código ({nombres}). Dime cuál")
            continue
        p = elegidos[0]
        if p["id"] in nuevas:
            nuevas[p["id"]]["cantidad"] += cant
        else:
            nuevas[p["id"]] = {"product_id": p["id"], "product_name": p["display_name"], "stock_actual": int(p["qty_available"]),
                               "cantidad": cant, "precio": p["lst_price"]}
    if not nuevas:
        return "❌ No encontré ninguno de esos códigos:\n" + "\n".join(problemas)
    for venta in nuevas.values():
        agregar_venta_pendiente(usuario, venta)   # un producto que ya estaba en el lote se corrige, no se duplica
    resp = vista_previa_venta(usuario)
    if problemas:
        resp = "⚠️ No pude agregar:\n" + "\n".join(problemas) + "\nSi confirmas, solo se venden las de abajo.\n\n" + resp
    return resp

def es_codigo_producto(texto):
    """Detecta códigos: dígitos solos, letras+dígitos, o número-guión-número (13-122)."""
    texto = texto.strip()
    return bool(re.fullmatch(r"[A-Za-z]{1,4}-?\d{1,6}|\d{3,8}|\d{1,6}-\d{1,6}", texto))

def codigo_sin_ruido(texto):
    """Código dictado con signos, puntos o espacios ('13 258', '13.258', '$13,258', '1 3 3 3 7') → '13258'.
    Solo si el texto son únicamente dígitos y separadores (3 a 12 dígitos); si no, None."""
    texto = (texto or "").strip()
    if re.fullmatch(r"[\s$\d.,;:\-–—_/·'’]+", texto):
        digitos = re.sub(r"\D", "", texto)
        if 3 <= len(digitos) <= 12:
            return digitos
    return None

# Los nombres de producto van en singular ("superior", "radiador"); el usuario o el modelo a veces escriben el plural.
_PLURALES = {
    "superiores": "superior", "inferiores": "inferior", "entradas": "entrada", "salidas": "salida",
    "mangueras": "manguera", "radiadores": "radiador", "tapas": "tapa", "codos": "codo", "termostatos": "termostato",
    "bombas": "bomba", "ventiladores": "ventilador", "tarros": "tarro", "reservas": "reserva", "térmicos": "térmico",
}

def extraer_palabras(texto):
    texto = texto.lower()
    palabras = re.findall(r"[\w/]+", texto)
    return [_PLURALES.get(p, p) for p in palabras if p not in STOP_WORDS and len(p) > 1]

def buscar_productos(query, atributos=None, categoria=None, limit=5, relajar_categoria=True):
    """Búsqueda inteligente con filtro opcional por categoría.
    relajar_categoria: si nada coincide, listar productos de la categoría (no se usa para fotos: serían fotos al azar)."""
    query = (query or "").strip()

    # Código dictado con ruido ("13 258", "13.258", "$13,258", "1 3 3 3 7"):
    # primero tal cual (hay códigos reales con guiones, p. ej. 8-98024684-0), luego solo dígitos
    limpio = codigo_sin_ruido(query)
    if limpio and not es_codigo_producto(query):
        for candidato in dict.fromkeys([query, limpio]):
            productos = odoo_search_read(
                "product.product",
                [["default_code", "ilike", candidato]],
                FIELDS_PRODUCTO,
                limit=limit,
            )
            if productos:
                return enriquecer_variantes(productos)

    # Caso: parece código de producto → buscar por default_code (ignora categoría)
    if es_codigo_producto(query):
        productos = odoo_search_read(
            "product.product",
            [["default_code", "ilike", query]],
            FIELDS_PRODUCTO,
            limit=limit,
        )
        if not productos and "-" in query:
            # Intentar sin guion
            productos = odoo_search_read(
                "product.product",
                [["default_code", "ilike", query.replace("-", "")]],
                FIELDS_PRODUCTO,
                limit=limit,
            )
        if productos:
            return enriquecer_variantes(productos)

    palabras = extraer_palabras(query) if query else []
    cat_id = CATEGORIAS.get(categoria.lower().strip()) if categoria else None

    if not palabras and not cat_id:
        return []

    # Filtros base: categoría + atributos
    domain_base = []
    if cat_id:
        domain_base.append(["categ_id", "child_of", cat_id])
    if atributos:
        separados = [["product_template_attribute_value_ids.name", "=ilike", attr] for attr in atributos]   # =ilike: sin distinguir mayúsculas ("baja" = "Baja")
        if len(atributos) >= 2:
            # Productos con atributos separados (tramos: diámetro…) O con UNA sola «medida» cuyo valor trae los dos juntos ("2, 26": corrugadas
            # desde la reorganización): vale cualquiera de las dos formas.
            combinado = ["product_template_attribute_value_ids.name", "=ilike", ", ".join(atributos)]
            alternativas = [combinado]
            if len(atributos) == 2 and atributos[0] != atributos[1]:
                # tapa pequeña: "Alta, 16" también se pide como "16 alta" (los dos órdenes valen; no existe ninguna medida «al revés»)
                alternativas.append(["product_template_attribute_value_ids.name", "=ilike", ", ".join(reversed(atributos))])
            domain_base.extend(["|"] * len(alternativas) + alternativas + ["&"] * (len(separados) - 1) + separados)
        else:
            # un solo dato ("corrugada de 2 pulgadas"): el valor suelto, o el comienzo / el final de una «medida» "2, 26"
            a = atributos[0]
            domain_base.extend(["|", "|", separados[0], ["product_template_attribute_value_ids.name", "=ilike", f"{a}, %"],
                                ["product_template_attribute_value_ids.name", "=ilike", f"%, {a}"]])

    productos = []
    exacta = True   # False si hubo que relajar la búsqueda (categoría completa / alguna de las palabras): lo que sale es «lo más parecido»

    # 1) Patrón ordenado: "inferior aveo" → name ilike '%inferior%aveo%'
    if len(palabras) > 1:
        patron = "%" + "%".join(palabras) + "%"
        productos = odoo_search_read(
            "product.product",
            domain_base + [["product_tmpl_id.name", "=ilike", patron]],
            FIELDS_PRODUCTO,
            limit=limit * 3,
        )

    # 2) Si no hay resultados: AND palabra por palabra, en cualquier orden
    if not productos:
        domain = domain_base + [["product_tmpl_id.name", "ilike", p] for p in palabras]
        productos = odoo_search_read("product.product", domain, FIELDS_PRODUCTO, limit=limit * 3)

    # 2b) La medida llegó como atributo («manguera siliconada 4 pulgadas») pero el producto NO tiene variantes: el 4" está en su NOMBRE. Se busca sin el
    # filtro de atributos y con la medida como una palabra más (antes caía a «lo primero de la categoría»: 8 mangueras de Ford).
    if not productos and atributos and palabras:
        dominio_sin_attr = [["categ_id", "child_of", cat_id]] if cat_id else []
        dominio_sin_attr += [["product_tmpl_id.name", "ilike", p] for p in palabras] + [["product_tmpl_id.name", "ilike", a] for a in atributos]
        productos = odoo_search_read("product.product", dominio_sin_attr, FIELDS_PRODUCTO, limit=limit * 3)

    # Fallback: si hay categoría pero no encontró nada, relajar el filtro de palabras
    if not productos and cat_id and palabras and relajar_categoria:
        productos = odoo_search_read(
            "product.product",
            [["categ_id", "child_of", cat_id]],
            FIELDS_PRODUCTO,
            limit=limit * 3,
        )
        exacta = False

    # Fallback: buscar por código si alguna palabra es número
    if not productos:
        for p in palabras:
            if re.fullmatch(r"\d+", p):
                productos = odoo_search_read(
                    "product.product",
                    [["default_code", "ilike", p]],
                    FIELDS_PRODUCTO,
                    limit=limit,
                )
                if productos:
                    break

    # Fallback: OR si nada coincide con AND
    if not productos and len(palabras) > 1:
        domain_or = []
        for i, p in enumerate(palabras):
            if i < len(palabras) - 1:
                domain_or.append("|")
            domain_or.append(["product_tmpl_id.name", "ilike", p])
        productos = odoo_search_read("product.product", domain_or, FIELDS_PRODUCTO, limit=limit * 3)
        exacta = False

    if not productos:
        return []

    productos = enriquecer_variantes(productos)
    for p in productos:
        p["_exacta"] = exacta

    # Score y orden
    for p in productos:
        score = 0
        name_lower = (p.get("display_name") or "").lower()
        code_lower = (p.get("default_code") or "").lower()
        for palabra in palabras:
            if palabra in name_lower:
                score += 2
            if code_lower and palabra in code_lower:
                score += 3
            for _, val in p.get("_variantes", []):
                if palabra in val.lower():
                    score += 1
        for a in (atributos or []):
            if re.search(rf"(?<![\d./]){re.escape(str(a).lower())}\s*(?:\"|”|''|pulgadas?\b)", name_lower):
                score += 3   # «4"» / «4 pulgadas» en el nombre: la medida pedida como medida, no como un 4 cualquiera (1.4, 2004…)
        p["_score"] = score

    productos.sort(key=lambda x: -x.get("_score", 0))
    return productos[:limit]

# ──────────────────────────────────────────
# FORMATEO
# ──────────────────────────────────────────

def formatear_lista_productos(productos, query=""):
    if not productos:
        return f"❌ No encontré productos para *{query}*."

    respuesta = f"🔍 *{query}*\n\n" if query else ""
    for i, p in enumerate(productos, 1):
        stock = int(p["qty_available"])
        stock_txt = f"{stock} unidad(es)" if stock > 0 else "⚠️ Sin stock"
        codigo = p.get("default_code", "")
        respuesta += f"{i}. *{p['display_name']}*"
        if codigo:
            respuesta += f" (Cód: {codigo})"
        respuesta += f"\n   💰 ${p['lst_price']:,.0f} | 📦 {stock_txt}\n"
    return respuesta.strip()

# ──────────────────────────────────────────
# OPERACIONES DE INVENTARIO
# ──────────────────────────────────────────

def get_ubicacion_interna():
    ubicaciones = odoo_search_read(
        "stock.location",
        [["usage", "=", "internal"]],
        ["id"],
        limit=1,
    )
    return ubicaciones[0]["id"] if ubicaciones else None

MAPA_VARIANTES_FILE = DATA_DIR / "mapa_variantes.json"
_mapa_variantes = {"mtime": None, "mapa": {}}

def variante_vigente(product_id):
    """Producto que HOY representa a `product_id`. Al reorganizar las corrugadas (una sola «medida» con precio propio) sus variantes viejas se
    ARCHIVARON y el stock pasó a las nuevas; las ventas anteriores siguen apuntando a las viejas. `data/mapa_variantes.json` = {viejo: nuevo};
    sin archivo o sin ese producto, devuelve el mismo id."""
    try:
        mtime = MAPA_VARIANTES_FILE.stat().st_mtime
    except OSError:
        return product_id
    if _mapa_variantes["mtime"] != mtime:
        _mapa_variantes["mapa"] = {int(k): int(v) for k, v in json.loads(MAPA_VARIANTES_FILE.read_text()).items()}
        _mapa_variantes["mtime"] = mtime
    return _mapa_variantes["mapa"].get(product_id, product_id)

def cantidad_en_bodega(product_id):
    """Existencias reales en la bodega según los registros de stock (sirve también para productos ARCHIVADOS, que qty_available no ve)."""
    return sum(q["quantity"] for q in odoo_search_read("stock.quant", [["product_id", "=", product_id], ["location_id.usage", "=", "internal"]], ["quantity"]))

def trasladar_stock(origen, destino, cantidad):
    """Pasa `cantidad` unidades del producto `origen` (archivado) al `destino`, con ajustes de inventario en el historial."""
    ajustar_stock(destino, get_stock_actual(destino) + cantidad)
    ajustar_stock(origen, cantidad_en_bodega(origen) - cantidad)

def get_stock_actual(product_id):
    productos = odoo_search_read(
        "product.product",
        [["id", "=", product_id]],
        ["qty_available"],
        limit=1,
    )
    return int(productos[0]["qty_available"]) if productos else 0

def ajustar_stock(product_id, cantidad_objetivo):
    """Deja el stock del producto EXACTAMENTE en `cantidad_objetivo`, como lo hace la pantalla de inventario de Odoo: con el modo de
    inventario activado, escribir `inventory_quantity_auto_apply` hace que Odoo cree el movimiento "Cantidad de producto actualizada"
    (queda en el historial con fecha y usuario). Antes se escribía `quantity` directo en el registro de existencias: el número quedaba
    bien pero SIN movimiento. Comprueba en Odoo que el registro quedó con esa cantidad y, si no, lanza un error."""
    loc_id = get_ubicacion_interna()
    if not loc_id:
        return False
    ctx = {"inventory_mode": True}
    filtro = [["product_id", "=", product_id], ["location_id", "=", loc_id]]
    quants = odoo_search_read("stock.quant", filtro, ["id"], limit=1)
    if quants:
        odoo_call("stock.quant", "write", [quants[0]["id"]], {"inventory_quantity_auto_apply": cantidad_objetivo}, context=ctx)
    else:
        odoo_call("stock.quant", "create", {"product_id": product_id, "location_id": loc_id,
                                            "inventory_quantity_auto_apply": cantidad_objetivo}, context=ctx)
    quedo = odoo_search_read("stock.quant", filtro, ["quantity"], limit=1)
    if not quedo or abs(quedo[0]["quantity"] - cantidad_objetivo) > 1e-6:
        raise RuntimeError(f"Odoo no dejó el stock en {cantidad_objetivo} (quedó en {quedo[0]['quantity'] if quedo else 0})")
    return True

def cancelar_entregas_de_orden(orden_id):
    """Al confirmar una venta, Odoo crea una entrega. Como el bot resta el stock por su cuenta (nunca valida la entrega),
    esa entrega quedaba abierta para siempre y Odoo la contaba como 'reservada': el 'disponible' salía mal (hasta negativo).
    Cancelarla no mueve existencias. Si falla, solo se avisa en el log: nunca debe hacer fallar la venta."""
    try:
        uid, models = odoo_connect()
        entregas = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.picking", "search",
            [[["sale_id", "=", orden_id], ["state", "not in", ["done", "cancel"]]]],
        )
        if entregas:
            models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "stock.picking", "action_cancel", [entregas])
        return True
    except Exception as e:
        log.warning(f"No pude cancelar la entrega de la orden {orden_id}: {e}")
        return False

def odoo_call(model, method, *args, **kwargs):
    """Llama un método de Odoo por XML-RPC: odoo_call('stock.picking', 'button_validate', [id])."""
    uid, models = odoo_connect()
    return models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, model, method, list(args), kwargs or {})

def validar_picking(picking_id):
    """Pone como HECHAS las cantidades (iguales a las demandadas, también si no había stock suficiente: Odoo deja el stock en
    negativo, igual que hacía el bot) y valida la transferencia: Odoo mueve el stock. Lanza error si no queda en 'done'."""
    movs = odoo_search_read("stock.move", [["picking_id", "=", picking_id]], ["product_uom_qty"])
    if not movs:
        raise RuntimeError("la transferencia no tiene movimientos")
    for m in movs:
        odoo_write("stock.move", [m["id"]], {"quantity": m["product_uom_qty"], "picked": True})
    res = odoo_call("stock.picking", "button_validate", [picking_id])
    if res is not True:  # Odoo devuelve un asistente (pedido de confirmación) en vez de validar
        raise RuntimeError(f"Odoo pidió un paso extra ({res.get('res_model') if isinstance(res, dict) else res})")
    est = odoo_search_read("stock.picking", [["id", "=", picking_id]], ["state"], limit=1)
    if not est or est[0]["state"] != "done":
        raise RuntimeError(f"la transferencia quedó en estado '{est[0]['state'] if est else '?'}'")

def validar_entrega_de_orden(orden_id):
    """Modo 'odoo': marca como HECHA la entrega de la venta (Odoo resta el stock). Devuelve el id de la entrega."""
    entregas = odoo_search_read("stock.picking", [["sale_id", "=", orden_id], ["state", "not in", ["done", "cancel"]]], ["name"])
    if len(entregas) != 1:
        raise RuntimeError(f"se esperaba 1 entrega y hay {len(entregas)}")
    validar_picking(entregas[0]["id"])
    return entregas[0]["id"]

def registrar_venta_completa(product_id, cantidad):
    uid, models = odoo_connect()
    orden_id = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "create",
        [{"partner_id": CLIENTE_MOSTRADOR_ID}],
    )
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order.line", "create",
        [{"order_id": orden_id, "product_id": product_id, "product_uom_qty": cantidad}],
    )
    models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "sale.order", "action_confirm",
        [[orden_id]],
    )
    if MODO_ENTREGA == "odoo":
        # Forma correcta: se valida la entrega y Odoo resta el stock. Si algo falla, la orden se cancela y el stock no se toca.
        stock_antes = get_stock_actual(product_id)
        try:
            validar_entrega_de_orden(orden_id)
        except Exception as e:
            log.error(f"Entrega de la orden {orden_id} no se pudo validar: {e}")
            try:
                models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "sale.order", "action_cancel", [[orden_id]])
            except Exception as e2:
                log.error(f"Tampoco pude cancelar la orden {orden_id}: {e2}")
            raise RuntimeError(f"Odoo no pudo registrar la entrega (la venta se canceló y el stock no cambió): {e}")
        stock_despues = get_stock_actual(product_id)
        if stock_despues != stock_antes - cantidad:
            log.warning(f"Orden {orden_id}: el stock pasó de {stock_antes} a {stock_despues} (se esperaba {stock_antes - cantidad})")
        return orden_id
    cancelar_entregas_de_orden(orden_id)  # sin entrega abierta no hay unidades "reservadas" de más
    stock_actual = get_stock_actual(product_id)
    ajustar_stock(product_id, stock_actual - cantidad)
    return orden_id

def cancelar_orden_venta(orden_id):
    uid, models = odoo_connect()
    try:
        models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "sale.order", "action_cancel",
            [[orden_id]],
        )
        return True
    except Exception as e:
        log.error(f"Error cancelando orden {orden_id}: {e}")
        return False

def crear_producto(nombre, precio, cantidad, categoria_id, referencia=None, costo=None):
    vals = {
        "name": nombre,
        "list_price": precio,
        "categ_id": categoria_id,
        "type": "consu",
        "tracking": "none",
        "is_storable": True,
        "taxes_id": [(5, 0, 0)],
        "supplier_taxes_id": [(5, 0, 0)],
    }
    if referencia:
        vals["default_code"] = referencia
    if costo is not None:
        vals["standard_price"] = costo

    template_id = odoo_create("product.template", vals)

    if cantidad > 0:
        productos = odoo_search_read(
            "product.product",
            [["product_tmpl_id", "=", template_id]],
            ["id"],
            limit=1,
        )
        if productos:
            ajustar_stock(productos[0]["id"], cantidad)
    return template_id

def producto_existe_similar(nombre):
    palabras = extraer_palabras(nombre)
    if len(palabras) < 2:
        return []
    domain = [["name", "ilike", p] for p in palabras[:3]]
    return odoo_search_read("product.template", domain, ["id", "name"], limit=3)

def costo_actual(product_id):
    """Costo de compra (standard_price) de ese producto/variante, o None si no existe."""
    r = odoo_search_read("product.product", [["id", "=", product_id]], ["standard_price"], limit=1)
    return r[0]["standard_price"] if r else None

def info_variante(product_id):
    """¿Producto SIMPLE o una VARIANTE entre varias de la misma plantilla? En Odoo una plantilla (p. ej. «Manguera tramo negra») puede tener
    varias variantes (1230, 1232, 1237…): el nombre y el precio base son de la plantilla; el código, el costo y el stock son de cada variante;
    el precio de una variante = precio base + el «extra» de sus atributos. → dict o None si el producto no existe."""
    v = odoo_search_read("product.product", [["id", "=", product_id]],
                         ["product_tmpl_id", "lst_price", "price_extra", "default_code", "product_template_attribute_value_ids"], limit=1)
    if not v:
        return None
    v = v[0]
    t = odoo_search_read("product.template", [["id", "=", v["product_tmpl_id"][0]]], ["name", "list_price", "product_variant_count"], limit=1)[0]
    return {"tmpl_id": t["id"], "plantilla": t["name"], "n": t["product_variant_count"], "es_variante": t["product_variant_count"] > 1,
            "list_price": t["list_price"], "lst_price": v["lst_price"], "price_extra": v["price_extra"], "codigo": v["default_code"],
            "ptavs": v["product_template_attribute_value_ids"]}

def precios_de_plantilla(tmpl_id):
    return {r["id"]: r["lst_price"] for r in odoo_search_read("product.product", [["product_tmpl_id", "=", tmpl_id]], ["lst_price"])}

def plan_precio(product_id, nuevo_precio):
    """Cómo dejar el PRECIO DE VENTA de ese producto en `nuevo_precio` sin tocar a las demás variantes. → {"modo", ...} o {"error": texto}.
    - Producto simple: precio base de la plantilla (menos su extra, normalmente 0).
    - Variante con un valor de atributo que SOLO ella usa (tramos, codos, rollos, tapas grandes/medianas: 1 atributo): se cambia el «extra de
      precio» de ese valor. Escribir el precio base de la plantilla (como se hacía antes) movía a TODAS las variantes y dejaba a esta en
      base+extra, no en lo pedido (p. ej. la 1237: base $0 + extra $4,000).
    - Variante cuyos valores comparten otras variantes (corrugadas: diámetro × largo; tapa pequeña): no se puede aislar → error."""
    iv = info_variante(product_id)
    if iv is None:
        return {"error": "❌ No encuentro ese producto en Odoo."}
    if not iv["es_variante"]:
        return {"modo": "plantilla", "tmpl_id": iv["tmpl_id"], "list_price": nuevo_precio - iv["price_extra"], "antes_list_price": iv["list_price"], "antes": iv["lst_price"]}
    hermanas = odoo_search_read("product.product", [["product_tmpl_id", "=", iv["tmpl_id"]]], ["product_template_attribute_value_ids"])
    usos = Counter(i for h in hermanas for i in h["product_template_attribute_value_ids"])
    exclusivos = [i for i in iv["ptavs"] if usos[i] == 1]
    if not exclusivos:
        compartido = max((usos[i] for i in iv["ptavs"]), default=iv["n"])
        return {"error": (f"❌ No puedo cambiar el precio de UNA sola variante de *{iv['plantilla']}*: su precio sale de la suma de varios atributos "
                          f"(p. ej. diámetro + largo) y cada uno lo comparten hasta {compartido} variantes, así que cambiarlo movería también a otras. "
                          f"Hazlo desde Odoo (Producto → Variantes → extra de precio).")}
    ptav = odoo_search_read("product.template.attribute.value", [["id", "=", exclusivos[0]]], ["price_extra"], limit=1)[0]
    return {"modo": "extra", "tmpl_id": iv["tmpl_id"], "ptav_id": exclusivos[0], "extra_antes": ptav["price_extra"],
            "price_extra": ptav["price_extra"] + (nuevo_precio - iv["lst_price"]), "antes": iv["lst_price"], "n": iv["n"], "plantilla": iv["plantilla"]}

def validar_cambios(product_id, cambios):
    """Revisa en Odoo, ANTES de pedir confirmación, que lo pedido se pueda hacer. → texto de error o None."""
    if "nuevo_precio" in cambios:
        plan = plan_precio(product_id, cambios["nuevo_precio"])
        if "error" in plan:
            return plan["error"]
    if cambios.get("nuevo_codigo"):
        otros = odoo_search_read("product.product", [["default_code", "=", cambios["nuevo_codigo"]], ["id", "!=", product_id]], ["display_name"], limit=1)
        if otros:
            return f"❌ El código *{cambios['nuevo_codigo']}* ya lo tiene otro producto: {otros[0]['display_name']}. Los códigos no se repiten."
    return None

def aplicar_precio(product_id, nuevo_precio):
    """Cambia el precio de venta según `plan_precio` y COMPRUEBA en Odoo que ese producto quedó en lo pedido y que las demás variantes de la
    plantilla no se movieron; si no, deshace el cambio y lanza un error."""
    plan = plan_precio(product_id, nuevo_precio)
    if "error" in plan:
        raise RuntimeError(plan["error"].replace("❌ ", ""))
    antes = precios_de_plantilla(plan["tmpl_id"])
    if plan["modo"] == "plantilla":
        odoo_write("product.template", [plan["tmpl_id"]], {"list_price": plan["list_price"]})
    else:
        odoo_write("product.template.attribute.value", [plan["ptav_id"]], {"price_extra": plan["price_extra"]})
    despues = precios_de_plantilla(plan["tmpl_id"])
    bien = abs(despues.get(product_id, -1) - nuevo_precio) < 0.005 and all(abs(despues.get(i, -1) - p) < 0.005 for i, p in antes.items() if i != product_id)
    if not bien:
        if plan["modo"] == "plantilla":
            odoo_write("product.template", [plan["tmpl_id"]], {"list_price": plan["antes_list_price"]})
        else:
            odoo_write("product.template.attribute.value", [plan["ptav_id"]], {"price_extra": plan["extra_antes"]})
        raise RuntimeError("Odoo no dejó los precios como se esperaba; deshice el cambio de precio.")

def actualizar_producto(product_id, **kwargs):
    if kwargs.get("nuevo_nombre"):
        # el nombre es de la PLANTILLA: lo comparten todas sus variantes (la vista previa lo avisa)
        t = odoo_search_read("product.product", [["id", "=", product_id]], ["product_tmpl_id"], limit=1)
        if t:
            odoo_write("product.template", [t[0]["product_tmpl_id"][0]], {"name": kwargs["nuevo_nombre"]})

    if kwargs.get("nuevo_precio") is not None:
        aplicar_precio(product_id, kwargs["nuevo_precio"])

    if kwargs.get("nuevo_codigo"):
        # el código es de cada VARIANTE (la plantilla de un producto con variantes no tiene; escribirlo allí no hacía nada)
        odoo_write("product.product", [product_id], {"default_code": kwargs["nuevo_codigo"]})
        r = odoo_search_read("product.product", [["id", "=", product_id]], ["default_code"], limit=1)
        if not r or r[0]["default_code"] != kwargs["nuevo_codigo"]:
            raise RuntimeError(f"Odoo no guardó el código {kwargs['nuevo_codigo']}")

    if kwargs.get("nuevo_costo") is not None:
        # El costo (standard_price) es POR VARIANTE: en la plantilla Odoo solo lo copia cuando hay una única variante (p. ej. la 1237 es
        # una de 13 "Manguera tramo negra"), así que se escribe en el producto exacto y se comprueba.
        odoo_write("product.product", [product_id], {"standard_price": kwargs["nuevo_costo"]})
        leido = costo_actual(product_id)
        if leido is None or abs(leido - kwargs["nuevo_costo"]) > 0.005:
            raise RuntimeError(f"Odoo no guardó el costo {kwargs['nuevo_costo']} (quedó en {leido})")

    if kwargs.get("agregar_stock") is not None:
        stock_actual = get_stock_actual(product_id)
        ajustar_stock(product_id, stock_actual + kwargs["agregar_stock"])
    elif kwargs.get("establecer_stock") is not None:
        ajustar_stock(product_id, kwargs["establecer_stock"])

    return True

# ──────────────────────────────────────────
# RESUMEN DE VENTAS
# ──────────────────────────────────────────

def resumen_ventas_periodo(periodo):
    # "hoy" = desde la medianoche de COLOMBIA (antes era la de UTC: a las 7 p. m. el día se reiniciaba);
    # las fechas de Odoo están en UTC, así que se compara en UTC.
    ahora = ahora_utc()
    if periodo == "hoy":
        desde = medianoche_colombia_en_utc()
        etiqueta = "hoy"
    elif periodo == "semana":
        desde = ahora - timedelta(days=7)
        etiqueta = "los últimos 7 días"
    elif periodo == "mes":
        desde = ahora - timedelta(days=30)
        etiqueta = "los últimos 30 días"
    else:
        desde = medianoche_colombia_en_utc()
        etiqueta = "hoy"

    desde_str = desde.strftime("%Y-%m-%d %H:%M:%S")
    ordenes = odoo_search_read(
        "sale.order",
        [
            ["partner_id", "=", CLIENTE_MOSTRADOR_ID],
            ["date_order", ">=", desde_str],
            ["state", "in", ["sale", "done"]],
        ],
        ["id", "amount_total", "order_line"],
        order="date_order desc",
    )

    if not ordenes:
        return f"📊 No hay ventas registradas para {etiqueta}."

    total_ordenes = len(ordenes)
    total_dinero = sum(o["amount_total"] for o in ordenes)

    line_ids = []
    for o in ordenes:
        line_ids.extend(o.get("order_line", []))

    productos_vendidos = {}
    if line_ids:
        lines = odoo_search_read(
            "sale.order.line",
            [["id", "in", line_ids]],
            ["product_id", "product_uom_qty"],
        )
        for l in lines:
            name = l["product_id"][1] if l.get("product_id") else "?"
            productos_vendidos[name] = productos_vendidos.get(name, 0) + l["product_uom_qty"]

    top = sorted(productos_vendidos.items(), key=lambda x: -x[1])[:5]

    respuesta = f"📊 *Resumen — {etiqueta}*\n\n"
    respuesta += f"Ventas: {total_ordenes}\n"
    respuesta += f"Total: ${total_dinero:,.0f} COP\n"
    if top:
        respuesta += "\n*Top productos:*\n"
        for nombre, cant in top:
            respuesta += f"• {nombre}: {int(cant)}\n"
    return respuesta.strip()

# ──────────────────────────────────────────
# DEMANDA PERDIDA
# ──────────────────────────────────────────

# ──────────────────────────────────────────
# LISTA DE PEDIDOS AL PROVEEDOR (antes «demanda perdida»; 2026-10-09)
# ──────────────────────────────────────────
# Cuando un cliente pide algo que no hay (no existe en el sistema o tiene stock 0), Diego lo anota para pedírselo al proveedor. Estados de cada fila:
# `pendiente` (falta pedirlo), `pedido` (ya se lo pidió al proveedor, esperando que llegue), `atendido` (llegó o lo quitó), `duplicado` (el mismo aviso
# anotado dos veces en segundos). La `clave` agrupa lo mismo dicho de distinta forma («corrugada 1 1/2 x 28» = «corrugada de 1 1/2 por 28»).

_STOP_CLAVE = {"de", "del", "la", "el", "los", "las", "para", "por", "x", "un", "una", "con", "y", "en", "al", "que", "pedido", "pidieron", "cliente"}
VENTANA_DUPLICADO = 180   # segundos: el mismo pedido anotado dos veces seguidas cuenta una sola vez

def _tokens_clave(texto):
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    palabras = [w for w in re.findall(r"\d+/\d+|[a-z0-9]+", t) if w not in _STOP_CLAVE]
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w[0].isdigit() else w for w in palabras]   # plural → singular

def clave_faltante(nombre, product_id=None):
    """Mismo producto de Odoo → misma clave; sin producto, las mismas palabras (sin orden, sin 'de/por', en singular)."""
    if product_id:
        return f"p{int(product_id)}"
    return "n:" + " ".join(sorted(set(_tokens_clave(nombre))))

def migrar_demanda_legacy():
    """Las filas anteriores a la lista de pedidos no tienen clave: se les calcula, y un mismo aviso repetido en segundos (había 3 pares) queda como duplicado."""
    conn = sqlite3.connect(DEMAND_DB)
    try:
        filas = conn.execute("SELECT id, fecha, nombre, product_id, clave, estado FROM demanda ORDER BY id").fetchall()
        ultimo = {}
        for i, fecha, nombre, pid, clave, estado in filas:
            if clave is None:
                clave = clave_faltante(nombre, pid)
                conn.execute("UPDATE demanda SET clave=?, estado=COALESCE(estado, 'pendiente') WHERE id=?", (clave, i))
            if (estado or "pendiente") == "pendiente":
                previo = ultimo.get(clave)
                if previo and abs((datetime.fromisoformat(fecha) - datetime.fromisoformat(previo)).total_seconds()) < VENTANA_DUPLICADO:
                    conn.execute("UPDATE demanda SET estado='duplicado' WHERE id=?", (i,))
                else:
                    ultimo[clave] = fecha
        conn.commit()
    finally:
        conn.close()

migrar_demanda_legacy()

def anotar_faltante(nombre, categoria=None, codigo=None, unidades=1, product_id=None, motivo=""):
    """Anota un pedido en la lista. → (veces, duplicado): cuántos pedidos pendientes lleva ese producto y si este ya estaba anotado hace segundos."""
    nombre = (nombre or "").strip()
    clave = clave_faltante(nombre, product_id)
    ahora = ahora_utc()
    conn = sqlite3.connect(DEMAND_DB)
    try:
        previo = conn.execute("SELECT fecha FROM demanda WHERE clave=? AND estado='pendiente' ORDER BY id DESC LIMIT 1", (clave,)).fetchone()
        duplicado = bool(previo and (ahora - datetime.fromisoformat(previo[0])).total_seconds() < VENTANA_DUPLICADO)
        if not duplicado:
            conn.execute("INSERT INTO demanda (fecha, nombre, categoria, codigo, unidades, product_id, motivo, estado, clave) VALUES (?, ?, ?, ?, ?, ?, ?, 'pendiente', ?)",
                         (ahora.isoformat(), nombre, categoria, codigo, max(int(unidades or 1), 1), product_id, motivo, clave))
            conn.commit()
        veces = conn.execute("SELECT COUNT(*) FROM demanda WHERE clave=? AND estado='pendiente'", (clave,)).fetchone()[0]
    finally:
        conn.close()
    return veces, duplicado

def texto_anotado(nombre, veces, duplicado, unidades=1):
    if duplicado:
        return f"📋 Ya lo tenía anotado hace un momento: *{nombre}*"
    t = f"📋 Anotado en la lista de pedidos: *{nombre}*"
    if unidades and unidades > 1:
        t += f"\n{unidades} unidades pedidas"
    if veces > 1:
        t += f"\nYa van {veces} pedidos de esto."
    return t + "\n(Dime «qué me falta pedir» para ver la lista.)"

def grupos_faltantes(desde=None):
    """Pedidos abiertos agrupados por producto. → (pendientes, pedidos): listas de dicts {nombre, categoria, product_id, veces, unidades, ultima, ids_pend, ids_ped}."""
    conn = sqlite3.connect(DEMAND_DB)
    try:
        q = "SELECT id, fecha, nombre, categoria, unidades, product_id, estado, clave, fecha_estado FROM demanda WHERE estado IN ('pendiente', 'pedido')"
        params = []
        if desde:
            q += " AND fecha >= ?"; params.append(desde)
        filas = conn.execute(q + " ORDER BY id", params).fetchall()
    finally:
        conn.close()
    grupos = {}
    for i, fecha, nombre, cat, uni, pid, estado, clave, f_estado in filas:
        g = grupos.setdefault(clave, {"nombre": nombre, "categoria": cat, "product_id": pid, "unidades": 0, "veces": 0, "ultima": fecha, "ids_pend": [], "ids_ped": [], "pedido_el": None})
        g["nombre"], g["ultima"] = nombre, max(g["ultima"], fecha)
        if estado == "pedido" and f_estado:
            g["pedido_el"] = max(g["pedido_el"] or f_estado, f_estado)
        g["product_id"] = g["product_id"] or pid
        (g["ids_pend"] if estado == "pendiente" else g["ids_ped"]).append(i)
        if estado == "pendiente":
            g["veces"] += 1; g["unidades"] += uni or 1
    pend = [g for g in grupos.values() if g["ids_pend"]]
    pend.sort(key=lambda g: g["ultima"], reverse=True); pend.sort(key=lambda g: -g["veces"])   # lo más pedido primero; a igual cantidad, lo más reciente
    ped = sorted([g for g in grupos.values() if not g["ids_pend"]], key=lambda g: g["pedido_el"] or g["ultima"], reverse=True)
    for g in ped:
        g["veces"] = len(g["ids_ped"])
    return pend, ped

def marcar_faltantes(ids, estado):
    """Cambia el estado de esas filas (solo las que siguen abiertas). → cuántas cambió."""
    if not ids:
        return 0
    conn = sqlite3.connect(DEMAND_DB)
    try:
        marcas = ",".join("?" * len(ids))
        cur = conn.execute(f"UPDATE demanda SET estado=?, fecha_estado=? WHERE id IN ({marcas}) AND estado IN ('pendiente', 'pedido')", [estado, ahora_utc().isoformat()] + list(ids))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

def cerrar_faltantes_producto(product_id):
    """Llegó stock de ese producto: sus pedidos abiertos se cierran. → cuántos pedidos eran."""
    conn = sqlite3.connect(DEMAND_DB)
    try:
        n = conn.execute("SELECT COUNT(*) FROM demanda WHERE product_id=? AND estado IN ('pendiente', 'pedido')", (product_id,)).fetchone()[0]
        if n:
            conn.execute("UPDATE demanda SET estado='atendido', fecha_estado=? WHERE product_id=? AND estado IN ('pendiente', 'pedido')", (ahora_utc().isoformat(), product_id))
            conn.commit()
        return n
    finally:
        conn.close()

def _hace_cuanto(iso):
    dias = ((ahora_utc() - UTC_MENOS_COLOMBIA).date() - (datetime.fromisoformat(iso) - UTC_MENOS_COLOMBIA).date()).days
    return "hoy" if dias <= 0 else "ayer" if dias == 1 else f"hace {dias} días"

LISTA_PEDIDOS = {}   # usuario → {"grupos": {n: grupo}, "ts": epoch}: la última lista que se le mostró (para «ya pedí el 2», «quita el 3»; en memoria)

def texto_lista_pedidos(usuario, periodo=None):
    desde = None
    if periodo == "hoy":
        desde = medianoche_colombia_en_utc().isoformat()
    elif periodo in ("semana", "mes", "año"):
        desde = (ahora_utc() - timedelta(days=7 if periodo == "semana" else 30 if periodo == "mes" else 365)).isoformat()
    pend, ped = grupos_faltantes(desde)
    # Lo que ya tiene stock en Odoo (llegó por otro camino, p. ej. se cargó desde Odoo) se quita solo
    llegaron = []
    ids_odoo = sorted({g["product_id"] for g in pend + ped if g["product_id"]})
    if ids_odoo:
        try:
            con_stock = {p["id"] for p in odoo_search_read("product.product", [["id", "in", ids_odoo], ["qty_available", ">", 0]], ["id"])}
        except Exception as e:
            log.warning(f"Lista de pedidos: no pude consultar el stock en Odoo ({e})")
            con_stock = set()
        for g in list(pend) + list(ped):
            if g["product_id"] in con_stock:
                marcar_faltantes(g["ids_pend"] + g["ids_ped"], "atendido")
                llegaron.append(g["nombre"])
                (pend if g in pend else ped).remove(g)
    numeros, lineas, n = {}, [], 0
    def fila(g, n, sufijo):
        cat = f" ({g['categoria']})" if g.get("categoria") else ""
        veces = f"{g['veces']} pedido{'s' if g['veces'] != 1 else ''}"
        if g["unidades"] > g["veces"] and sufijo == "ult":
            veces += f" · {g['unidades']} unidades"
        cuando = f"última {_hace_cuanto(g['ultima'])}" if sufijo == "ult" else f"pedido {_hace_cuanto(g['pedido_el'] or g['ultima'])}"
        return f"{n}. {g['nombre']}{cat}\n   {veces} · {cuando}"
    if pend:
        lineas.append(f"📋 *Lista de pedidos al proveedor* ({len(pend)})\n")
        for g in pend[:30]:
            n += 1; numeros[n] = g; lineas.append(fila(g, n, "ult"))
        if len(pend) > 30:
            lineas.append(f"… y {len(pend) - 30} más")
    if ped:
        lineas.append(("\n" if lineas else "") + "✅ *Ya pedidos* (esperando que lleguen):")
        for g in ped[:15]:
            n += 1; numeros[n] = g; lineas.append(fila(g, n, "ped"))
    if not lineas:
        lineas.append("📋 La lista de pedidos está vacía. 👍")
    else:
        lineas.append("\nPara actualizarla: «ya pedí el 1 y el 3» · «ya pedí todo» · «quita el 2»")
    if llegaron:
        lineas.append("\n🎉 Ya hay stock y los quité de la lista: " + ", ".join(llegaron))
    LISTA_PEDIDOS[usuario] = {"grupos": numeros, "ts": time.time()}
    return "\n".join(lineas)

_PIDIO_YA = re.compile(r"(?i)\bya\s+(?:lo\s+|la\s+|los\s+|las\s+)?(?:ped[ií]|pedimos|orden[eé]|encarg\w+)\b|\b(?:ped[ií]|pedimos|orden[eé]|encargu[eé])\b")
_QUITAR = re.compile(r"(?i)\b(?:quita\w*|borra\w*|elimina\w*|descarta\w*|tacha\w*)\b|\bya\s+no\s+(?:lo|la|los|las)?\s*(?:necesit\w+|quier\w+|piden\w*)\b")

def atajo_lista_pedidos(usuario, texto):
    """Con la lista a la vista: «ya pedí el 1 y el 3», «ya pedí todo» (pasan a «ya pedidos»), «quita el 2», «ya llegó el 4» (salen de la lista)."""
    est = LISTA_PEDIDOS.get(usuario)
    t = texto or ""
    if not est or time.time() - est["ts"] > 1800 or "?" in t or len(t.split()) > 14 or pide_imagen(t) or re.search(r"(?i)\bllegaron?\b|\bllegó\b", t):
        return None   # «llegaron 5» es stock que entró, «foto del 2» es una foto: nada de eso toca la lista
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") in _TIPOS_EJECUTABLES:
        return None   # con una venta/actualización por confirmar, "quita el 2" es de esa
    quitar, pidio = bool(_QUITAR.search(t)), bool(_PIDIO_YA.search(t))
    if quitar == pidio:
        return None
    todos = bool(re.search(r"(?i)\btod[oa]s?\b", t)) and pidio
    nums = [int(x) for x in re.findall(r"(?<![\d/])\d{1,2}(?![\d/])", t)]
    if not nums and not todos:
        return None
    grupos = est["grupos"]
    if todos:
        elegidos = [n for n, g in grupos.items() if g["ids_pend"]]
    else:
        malos = [n for n in nums if n not in grupos]
        if malos:
            return f"No hay un {malos[0]} en la lista (1 a {max(grupos) if grupos else 0}). Pídeme «qué me falta pedir» para verla de nuevo."
        elegidos = sorted(set(nums))
    if not elegidos:
        return "No hay nada pendiente por marcar."
    if pidio:
        for n in elegidos:
            marcar_faltantes(grupos[n]["ids_pend"], "pedido")
        return "✅ Marcados como pedidos al proveedor:\n" + "\n".join(f"{n}. {grupos[n]['nombre']}" for n in elegidos)
    for n in elegidos:
        marcar_faltantes(grupos[n]["ids_pend"] + grupos[n]["ids_ped"], "atendido")
    return "🗑️ Quité de la lista:\n" + "\n".join(f"{n}. {grupos[n]['nombre']}" for n in elegidos)

OFERTA_FALTANTE = {}   # usuario → lo que el bot acaba de ofrecer anotar (en memoria; vale 10 min y solo para el SIGUIENTE mensaje)

def ofrecer_faltante(usuario, nombre, categoria=None, codigo=None, product_id=None, motivo="", intro=""):
    """Tras una búsqueda sin resultado o sin stock: pregunta si lo anota. No ofrece con una venta/actualización por confirmar (esa "sí" es de ella). → texto o ''."""
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") in _TIPOS_EJECUTABLES:
        return ""
    OFERTA_FALTANTE[usuario] = {"nombre": nombre, "categoria": categoria, "codigo": codigo, "product_id": product_id, "motivo": motivo, "ts": time.time()}
    return f"\n\n📋 {intro}¿Lo anoto en la lista de pedidos para el proveedor? Responde *sí* o *no*."

def atajo_faltante(usuario, texto):
    """El «sí» / «no» a «¿Lo anoto en la lista de pedidos?». Vale solo para el mensaje siguiente: cualquier otra cosa olvida la oferta y sigue normal."""
    of = OFERTA_FALTANTE.pop(usuario, None)
    if not of or time.time() - of["ts"] > 600:
        return None
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") in _TIPOS_EJECUTABLES:
        return None
    if _CONFIRMA.match(texto or ""):
        veces, dup = anotar_faltante(of["nombre"], of["categoria"], of["codigo"], 1, of["product_id"], of["motivo"])
        return texto_anotado(of["nombre"], veces, dup)
    if _CANCELA.match(texto or ""):
        return "Listo, no lo anoto."
    return None

SUGERENCIA_CODIGO = {}      # usuario → código que el bot propuso en lugar del escrito (memoria; 10 min; solo para el SIGUIENTE mensaje)
SUGERENCIA_EJECUTADA = {}   # usuario → la llamada que ejecutó el «sí» (para dejar el historial como si el modelo la hubiera hecho)
_CODIGO_CON_RUIDO = re.compile(r"[\s$\d.,;:_·'’]+")

def sugerencia_codigo(usuario, nombre, args, query, productos):
    """Un código escrito de más («289.15.34» por 891534: el 2 sobra). Si lo escrito son SOLO dígitos y separadores (5 a 9 dígitos), ningún resultado lo contiene
    como código y quitándole UN dígito sale el código de exactamente UN producto, se pregunta «¿Quisiste decir…?» en lugar de listar lo que la búsqueda por palabras
    encontró al azar. «sí» (el siguiente mensaje) repite la consulta con el código corregido: una búsqueda o la vista previa de la venta (que vuelve a pedir
    «¿Confirmas?»). → texto de la pregunta, o '' si no aplica (entonces todo sigue como antes)."""
    texto = (query or "").strip()
    if nombre not in ("buscar", "vender") or args.get("atributos") or args.get("categoria") or not _CODIGO_CON_RUIDO.fullmatch(texto):
        return ""
    digitos = re.sub(r"\D", "", texto)
    if not 5 <= len(digitos) <= 9 or any(digitos in (p.get("default_code") or "") for p in productos or []):
        return ""
    candidatos = sorted({digitos[:i] + digitos[i + 1:] for i in range(len(digitos))} - {digitos})
    try:
        halladas = odoo_search_read("product.product", [["default_code", "in", candidatos]], FIELDS_PRODUCTO, limit=3)
    except Exception as e:
        log.warning(f"sugerencia_codigo: no pude buscar {candidatos}: {e}")
        return ""
    if len(halladas) != 1:
        return ""   # ninguno, o varios posibles: no se adivina
    p = halladas[0]
    codigo = p.get("default_code")
    base = f"❌ No encontré el código *{texto}*.\n\n💡 ¿Quisiste decir *{p['display_name']}*? (lo que escribiste tiene un dígito de más)"
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") in _TIPOS_EJECUTABLES:
        return base + f"\nSi es ese, escríbeme el código *{codigo}*."   # ese «sí» sería de lo que ya está por confirmar
    SUGERENCIA_CODIGO[usuario] = {"nombre": nombre, "param": _PARAM_CONSULTA[nombre], "args": dict(args), "codigo": codigo, "ts": time.time()}
    OFERTA_FALTANTE.pop(usuario, None)
    accion = "preparo la venta de ese producto" if nombre == "vender" else "te lo muestro"
    return base + f"\nResponde *sí* y {accion}, o *no*."

def atajo_sugerencia(usuario, texto):
    """El «sí» / «no» a «¿Quisiste decir…?». Vale solo para el mensaje siguiente: cualquier otra cosa olvida la propuesta y sigue normal."""
    sug = SUGERENCIA_CODIGO.pop(usuario, None)
    if not sug or time.time() - sug["ts"] > 600:
        return None
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") in _TIPOS_EJECUTABLES:
        return None
    if _CANCELA.match(texto or ""):
        return "Listo, no lo busco."
    if not _CONFIRMA.match(texto or ""):
        return None
    args = dict(sug["args"])
    args[sug["param"]] = sug["codigo"]
    args.pop("opcion_lista", None)
    if sug["nombre"] == "vender":
        resultado = handler_vender(usuario, args)
        respuesta = vista_previa_venta(usuario) if resultado == "__BATCH_VENTA__" else resultado
    else:
        resultado = respuesta = handler_buscar(usuario, args)
    SUGERENCIA_EJECUTADA[usuario] = {"nombre": sug["nombre"], "args": args, "resultado": resultado}
    return respuesta

def descripcion_busqueda(query, categoria, atributos):
    """Lo que se anota cuando la búsqueda no encontró nada: categoría + palabras + medidas ('mangueras corrugadas 1 1/2 x 28')."""
    partes = [p for p in (categoria, (query or "").strip()) if p]
    if atributos:
        partes.append(" x ".join(str(a) for a in atributos))
    return " ".join(partes).strip() or (query or "").strip()

# ──────────────────────────────────────────
# TEXTO DE AYUDA
# ──────────────────────────────────────────

TEXTO_AYUDA = """🤖 *Comandos de Radibot*

📦 *Búsqueda de productos*
Consulta precio, stock o disponibilidad. Puedes buscar por nombre, modelo, marca o código.
_Ejemplos:_
- "busca el radiador de aveo"
- "manguera del corsa"
- "tienes bomba de agua de hilux?"
- "Manguera 5533"
📷 *Fotos:* solo se envían si las pides.
- "dame la imagen de 5490"
- "quiero ver la foto de las mangueras superiores del aveo" (hasta 5)
- "foto de la 2" (tras una lista, la opción 2)

💰 *Registrar venta*
Descuenta el stock y crea una orden de venta en Odoo. Te pide confirmación antes de aplicar.
_Ejemplos:_
- "se vendió 1 radiador aveo"
- "vendí 2 mangueras 7107"
- "salió una tapa pequeña alta 16"
- Varios a la vez: "se vendió la 5490, 5492, 5491" o "2 del 5490 y 1 del 5492" (una sola confirmación)
- Si te equivocas en la cantidad antes de confirmar, corrígela: "perdón, fueron 2" (cambia la cantidad, no suma otra venta)

↩️ *Deshacer última venta*
Para cuando te EQUIVOCASTE al registrar la última venta: la cancela y devuelve el stock.
_Ejemplos:_
- "deshacer última venta"
- "me equivoqué con la última"

🔁 *Devolución de un cliente (de cualquier venta, no solo la última)*
El producto vuelve al stock y esa venta se cancela, o baja de cantidad si devuelve solo una parte. Te muestra la venta y pide confirmación. Si hay varias ventas del mismo producto, te pregunta cuál.
_Ejemplos:_
- "el cliente devolvió la 5490"
- "me devolvieron 1 de la 4504" (compró 2 y devuelve 1)
- "devolución de la manguera del aveo"
- Varios productos: "devolvieron una 5490 y una 5492" (una sola confirmación para todos)
Si no dices cuántas, asume 1; lo ves en la confirmación y puedes corregirlo ("no, eran las dos").

📈 *Actualizar producto existente*
Modifica precio, costo, nombre, código o stock de un producto que ya existe. (Precio = lo que paga el cliente; costo = lo que pagó la tienda.)
_Ejemplos:_
- "llegaron 5 radiadores aveo" (suma 5 al stock)
- "el stock del 5533 es 20" (deja exactamente 20)
- "el precio de la 7107 es 50000"
- "el costo de la 7107 es 30000"
- "el código de la manguera corsa es 1234"
- "cambia el nombre de la 5533 a Manguera Aveo 1.6"
- Precio y stock juntos: "actualiza la 13351, precio 40000 y stock 1". Antes de confirmar puedes corregir un dato ("el stock a 5") o quitarlo ("quita el precio").
- Productos con variantes (tramos, codos, rollos, tapas): el precio, costo, código y stock se cambian por variante ("el precio de la 1237 es 5000" solo cambia esa).
- Corrugadas: dime diámetro y largo ("de 2 y media por 40"). Si falta uno, te muestro las opciones con precio y stock y te pregunto cuál.

🆕 *Crear producto nuevo*
Agrega un producto que aún no existe en el inventario.
Necesitas: *nombre, precio, stock inicial, categoría* y opcionalmente *costo* y *código*.
(El precio es lo que paga el cliente; el costo es lo que pagó la tienda.)
Categorías disponibles: __CATEGORIAS__.
_Ejemplos:_
- "crea radiador chevrolet sail, precio 350000, costo 220000, stock 1, categoría radiadores"
- "crea bomba de agua mazda 3, precio 180000, costo 120000, stock 2, categoría bombas de agua, código BA-103"

📊 *Resumen de ventas*
Te muestra cuánto se vendió en un periodo y los productos top.
_Ejemplos:_
- "resumen de hoy"
- "ventas de la semana"
- "ventas del mes"

✅ *Confirmar*
Después de que el bot te pregunte "¿confirmas?" o te muestre una lista, responde:
- "sí" / "confirmo" para confirmar
- "la 1" / "el 2" para escoger una opción de la lista

📋 *Lista de pedidos (para el proveedor)*
Si buscas algo que no hay (o está en 0), te pregunto si lo anoto. También: "vinieron por radiador aveo y no había".
- "qué me falta pedir?"
- "ya pedí el 1 y el 3" · "quita el 2"
Cuando registras que llegó stock, se quita sola.

❓ Escribe *ayuda* para ver este mensaje"""
TEXTO_AYUDA = TEXTO_AYUDA.replace("__CATEGORIAS__", ", ".join(CATEGORIAS.keys()))

# ──────────────────────────────────────────
# HANDLERS DE HERRAMIENTAS
# ──────────────────────────────────────────

MAX_BUSQUEDA = 5
MENSAJE_HAY_MAS = "➕ Hay más resultados. Afina la búsqueda para verlos."

# usuario -> {"texto": str, "items": [{"tmpl_id", "caption"}]}. Un solo uso: lo consume el webhook al responder.
IMAGENES_A_ENVIAR = {}

def caption_producto(p):
    """'[código] nombre' + precio + stock, igual que en la lista de búsqueda."""
    stock = int(p["qty_available"])
    stock_txt = f"{stock} unidad(es)" if stock > 0 else "⚠️ Sin stock"
    return f"*{p['display_name']}*\n💰 ${p['lst_price']:,.0f} | 📦 {stock_txt}"

def preparar_envio_imagenes(usuario, productos, query, hay_mas):
    """Una foto por producto (con su descripción); los que no tienen foto se listan en texto."""
    tmpl_ids = [p["product_tmpl_id"][0] for p in productos if p.get("product_tmpl_id")]
    filas = odoo_search_read("product.template", [["id", "in", tmpl_ids], ["image_1920", "!=", False]], ["id"])
    con_foto = {r["id"] for r in filas}
    con = [p for p in productos if p.get("product_tmpl_id") and p["product_tmpl_id"][0] in con_foto]
    sin = [p for p in productos if p not in con]
    con.sort(key=lambda p: int(p["qty_available"]) <= 0)  # primero los que tienen stock; dentro de cada grupo, por relevancia

    partes = []
    if sin:
        lineas = [caption_producto(p).replace("\n", " — ") for p in sin]
        partes.append("📷 Sin foto cargada:\n" + "\n".join(f"• {l}" for l in lineas))
    if hay_mas:
        partes.append(MENSAJE_HAY_MAS)
    texto = "\n\n".join(partes)

    IMAGENES_A_ENVIAR[usuario] = {
        "texto": texto,
        "items": [{"tmpl_id": p["product_tmpl_id"][0], "caption": caption_producto(p)} for p in con],
    }
    # Resumen para el historial del modelo (lo que el usuario recibe)
    resumen = f"📷 {len(con)} foto(s) enviada(s) de *{query}*:\n" + "\n".join(f"• {p['display_name']}" for p in con)
    return resumen + (f"\n\n{texto}" if texto else "")

ATRIBUTOS_MEDIDA = ("medida corrugada", "medida codo negro", "medida codo rojo")   # un solo atributo con valores «diámetro, largo»

def partes_de_medida(p):
    """(diámetro, largo) de una manguera corrugada o de un codo negro/rojo, sea UNA «medida» "2, 26" o (corrugadas, estructura anterior) los dos atributos
    separados. None si no lo es (el codo azul solo tiene diámetro; tapas, tramos, rollos… tampoco)."""
    v = dict(p.get("_variantes") or [])
    for k in ATRIBUTOS_MEDIDA:
        if k in v and ", " in v[k]:
            d, l = v[k].split(", ", 1)
            return d, l
    if "diámetro corrugada" in v and "longitud corrugada" in v:
        return v["diámetro corrugada"], v["longitud corrugada"]
    return None

def _num_medida(texto):
    """'1 1/2' → 1.5 (para ordenar diámetros y largos)."""
    try:
        total = 0.0
        for t in (texto or "").split():
            total += eval_fraccion(t)
        return total
    except Exception:
        return 0.0

def eval_fraccion(t):
    if "/" in t:
        a, b = t.split("/")
        return float(a) / float(b)
    return float(t)

_DIAMETROS = ["1", "1 1/4", "1 1/2", "1 3/4", "2", "2 1/4", "2 1/2", "2 3/4"]
_LARGOS = [str(n) for n in range(12, 44, 2)]
_LARGOS_PALABRA = {"doce": 12, "catorce": 14, "dieciseis": 16, "dieciocho": 18, "veinte": 20, "veintidos": 22, "veinticuatro": 24, "veintiseis": 26,
                   "veintiocho": 28, "treinta": 30, "treinta y dos": 32, "treinta y cuatro": 34, "treinta y seis": 36, "treinta y ocho": 38,
                   "cuarenta": 40, "cuarenta y dos": 42}
_PALABRA_ENTERO = {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4}
_FRACCION = {"media": 0.5, "cuarto": 0.25, "tres cuartos": 0.75}

# Productos cuya variante es UNA «medida» «diámetro, largo». `cant`: palabras que, pegadas a un número, lo vuelven CANTIDAD ("dos codos");
# `producto`: palabras que pueden quedar sueltas en la búsqueda; `excluir`: otros productos de la misma categoría que NO son de esta familia;
# `mezcla`: la búsqueda por nombre trae también otros productos (semicodos con «codo negro»), así que se miran solo las variantes con medida.
_DIAM_CODOS = ["1", "1 1/4", "1 1/2", "1 3/4", "2", "2 1/4", "2 1/2", "2 3/4", "3", "3 1/4", "3 1/2", "3 3/4", "4"]
FAMILIAS_MEDIDA = {
    "mangueras corrugadas": {"categoria": "mangueras corrugadas", "patron": r"corrugad", "excluir": None, "diametros": _DIAMETROS, "largos": _LARGOS,
                             "cant": r"(?:mangueras?|corrugadas?)", "producto": ("manguera", "mangueras", "corrugada", "corrugadas"),
                             "ejemplo": "de 2 y media por 30", "mezcla": False, "extra_q": ""},
    "codos": {"categoria": "codos", "patron": r"\bcodos?\b", "excluir": r"semi|reductor|\bele\b", "diametros": _DIAM_CODOS, "largos": ["12", "16", "20", "28"],
              "cant": r"(?:codos?)", "producto": ("codo", "codos"), "ejemplo": "de 2 y media por 20", "mezcla": True, "extra_q": "|negro|rojo"},
}

def familia_medida(categoria, texto=""):
    """Familia de «medida» a la que pertenece la categoría o el texto ('corrugada', 'codo negro'), o None. 'semicodo', 'reductor' y 'Ele' NO son de la
    familia de los codos (siguen con su diámetro suelto)."""
    cat = (categoria or "").lower().strip()
    for f in FAMILIAS_MEDIDA.values():
        if f["excluir"] and re.search(f["excluir"], texto or "", re.I):
            continue
        if cat == f["categoria"] or re.search(f["patron"], texto or "", re.I):
            return f
    return None

def _canon_medida(x):
    """2.5 → '2 1/2'; 2.0 → '2'; None si no es un múltiplo de 1/4."""
    entero, resto = int(x), round(x - int(x), 4)
    f = {0.0: "", 0.25: " 1/4", 0.5: " 1/2", 0.75: " 3/4"}.get(resto)
    return None if f is None else f"{entero}{f}"

def medidas_en_texto(texto, fam=None):
    """Diámetros y largos (de corrugadas o de codos, según `fam`) que el usuario DIJO, en cifras o en palabras ('de dos y media por veinte', '2.5 por 40', 'de 1 1/4').
    Un número pegado al producto ('dos corrugadas', '2 mangueras') es la CANTIDAD, no una medida. → ([diámetros], [largos]) en forma canónica."""
    fam = fam or FAMILIAS_MEDIDA["mangueras corrugadas"]
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = re.sub(r"\s+", " ", t)
    # largos en palabras (los más largos primero: 'treinta y dos' antes que 'treinta')
    for pal in sorted(_LARGOS_PALABRA, key=len, reverse=True):
        t = re.sub(rf"\b{pal}\b", str(_LARGOS_PALABRA[pal]), t)
    patron = re.compile(r"(?<![\w/.,])(?:(\d+(?:[.,]\d+)?)(?:\s+(\d/\d)|\s+y\s+(media|cuarto|tres cuartos))?|(\d/\d)|(uno|una|un|dos|tres|cuatro)(?:\s+y\s+(media|cuarto|tres cuartos))?)(?![\w/]|[.,]\d|\s*\d)")
    diam, largos = [], []
    for m in patron.finditer(t):
        siguiente = t[m.end():m.end() + 14]
        if re.match(rf"\s*{fam['cant']}\b", siguiente):
            continue   # "dos corrugadas": cantidad
        if m.group(5):
            v = float(_PALABRA_ENTERO[m.group(5)]) + (_FRACCION.get(m.group(6), 0.0) if m.group(6) else 0.0)
        elif m.group(4):
            a, b = m.group(4).split("/"); v = float(a) / float(b)
        else:
            v = float(m.group(1).replace(",", "."))
            if m.group(2):
                a, b = m.group(2).split("/"); v += float(a) / float(b)
            elif m.group(3):
                v += _FRACCION[m.group(3)]
        c = _canon_medida(v)
        if c in fam["diametros"]: diam.append(c)
        elif c in fam["largos"]: largos.append(c)
    return diam, largos

_RELLENO_MEDIDA = {"de", "por", "x", "y", "con", "pulgada", "pulgadas", "media", "cuarto", "cuartos", "tres", "un", "una", "uno", "dos", "la", "el", "las", "los",
                   "doce", "catorce", "dieciseis", "dieciocho", "veinte", "veintidos", "veinticuatro", "veintiseis", "veintiocho", "treinta", "cuarenta"}

def cantidad_en_texto(texto, fam=None):
    """'se vendieron dos corrugadas', 'vendí 2 mangueras': el número pegado al producto es la CANTIDAD. None si no dijo ninguna."""
    fam = fam or FAMILIAS_MEDIDA["mangueras corrugadas"]
    t = unicodedata.normalize("NFD", (texto or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    m = re.search(rf"\b(\d{{1,3}}|un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|doce)\s+{fam['cant']}\b", t)
    if not m:
        return None
    v = m.group(1)
    return int(v) if v.isdigit() else {"un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "doce": 12}[v]

def completar_medidas_desde_texto(nombre, args, texto):
    """'se vendió corrugada de dos' / 'codo negro de dos y media por veinte': el modelo dejó el 'dos' (en palabras) sin pasar a `atributos` y salió una
    lista de 5 de las 128 (o 36) medidas. Lo que el usuario DIJO manda: si el mensaje trae medidas de corrugadas o de codos negro/rojo, se ponen en
    `atributos` (diámetro primero, luego largo). Solo si hay un diámetro y/o un largo claros (si dijo dos diámetros, se deja al modelo). True si cambió algo."""
    if nombre not in ("vender", "actualizar", "buscar"):
        return False
    fam = familia_medida(args.get("categoria"), texto)
    if not fam:
        return False
    diam, largos = medidas_en_texto(texto, fam)
    if len(diam) > 1 or len(largos) > 1 or not (diam or largos):
        return False
    modelo = [a.strip() for a in (args.get("atributos") or [])]
    # si el modelo ya dio un valor válido para esa medida se respeta; lo que dijo el usuario RELLENA lo que el modelo dejó vacío
    d = next((a for a in modelo if a in fam["diametros"]), None) or (diam[0] if diam else None)
    l = next((a for a in modelo if a in fam["largos"]), None) or (largos[0] if largos else None)
    nuevo = [x for x in (d, l) if x]
    cambio = False
    if nuevo != modelo:
        args["atributos"] = nuevo
        cambio = True
    if not args.get("categoria"):
        args["categoria"] = fam["categoria"]
        cambio = True
    # La cantidad es la que el usuario dijo pegada al producto ("dos corrugadas"); si no dijo ninguna, es 1. El modelo a veces toma el largo por la
    # cantidad ("corrugada de una pulgada por doce" → 12 unidades).
    if nombre == "vender":
        cant = cantidad_en_texto(texto, fam)
        nueva_cant = cant if cant else (1 if args.get("cantidad") in {int(x) for x in largos} else None)
        if nueva_cant and args.get("cantidad") != nueva_cant:
            args["cantidad"] = nueva_cant
            cambio = True
    # Palabras de medida sueltas en la búsqueda ("uno y tres cuartos", "de dos") la estropean: si solo quedan medidas y el nombre del producto, va vacía
    campo = "query" if nombre == "buscar" else "producto_query"
    q = (args.get(campo) or "").strip()
    if q and args.get("atributos"):
        restos = [w for w in _palabras_sin_tilde(q) if w not in _RELLENO_MEDIDA and not re.fullmatch(r"\d+|\d/\d", w)]
        if all(w in fam["producto"] for w in restos):
            args[campo] = ""
            cambio = True
    return cambio

_COLOR_CODO = {"negr": "negro", "roj": "rojo", "azul": "azul"}

def completar_palabras_codo(nombre, args, texto):
    """El modelo a veces SUELTA una palabra que cambia de producto: 'actualiza el costo del codo rojo de 2 por 12' llegó sin «rojo» (y el bot preguntó
    cuál de los dos codos) y 'semicodo negro 2 1/2' llegó como «negro» (y salió la tabla del CODO negro, que no es lo que pidió). Lo que el usuario DIJO
    manda: en la categoría codos, el color («negro», «rojo», «azul») de un codo y la palabra «semicodo» se devuelven a la búsqueda si faltan. True si cambió algo."""
    if nombre not in ("vender", "actualizar", "buscar") or (args.get("categoria") or "").lower() != "codos":
        return False
    campo = "query" if nombre == "buscar" else "producto_query"
    q = (args.get(campo) or "").strip()
    t = " ".join(_palabras_sin_tilde(texto or ""))
    nuevo_q = q
    if re.search(r"\breductor(?:es)?\b", t):
        # los reductores («3 a 2 1/2») no tienen atributos: el modelo los pasaba como [3, 2 1/2] y salía una lista cualquiera de la categoría
        cambio = False
        if args.get("atributos"):
            args["atributos"] = None
            cambio = True
        if "reductor" not in q.lower():
            args[campo] = ("reductor " + q).strip()
            cambio = True
        return cambio
    if re.search(r"\bsemi ?codos?\b", t):
        if "semi" not in q.lower():
            nuevo_q = ("semicodo " + q).strip()
    elif familia_medida("codos", texto):
        colores = {_COLOR_CODO[c] for c in re.findall(r"\b(negr|roj|azul)(?:o|a|os|as|es)?\b", t)}
        if len(colores) == 1:
            color = colores.pop()
            if color not in q.lower():
                nuevo_q = (q + " " + color).strip()
    if nuevo_q != q:
        args[campo] = nuevo_q
        return True
    return False

_CONSULTA_VAGA = re.compile(r"(?i)^\s*[¿¡]?\s*(?:(?:tienes|tiene|tienen|tendr[aá]s|hay|manejas|busco|necesito|quiero|dame|me\s+das|me\s+puedes\s+dar|cu[aá]nto\s+(?:vale|valen|cuesta|cuestan)|precio\s+de)\s+)?(?:una\s+|unas\s+|la\s+|las\s+|el\s+|los\s+|alguna\s+|algun[ao]s?\s+)?(?:manguera[s]?\s+|codos?\s+)?(corrugada[s]?|(?:codos?\s+)?(?:negro|rojo)s?)\s*[?!.]*\s*$")

def atajo_corrugada_sin_medida(usuario, texto):
    """'se vendió corrugada' / 'se vendió codo negro' / 'tienes corrugada?' (sin diámetro ni largo): el modelo contestaba libremente sin consultar nada (o
    se quedaba en «¿te refieres a…?»). Se explica cómo se define y qué medidas hay. (El codo necesita el color: sin «negro» o «rojo» no se sabe cuál.)"""
    t = texto or ""
    if pide_imagen(t) or re.search(r"\d", t):
        return None
    consulta = _CONSULTA_VAGA.match(t)
    if "?" in t and not consulta:
        return None
    if not consulta and not _VERBO_VENTA.search(t):
        return None
    fam = familia_medida(None, t)
    if not fam:
        return None
    d, l = medidas_en_texto(t, fam)
    if d or l:
        return None
    para = "buscar" if consulta and not _VERBO_VENTA.search(t) else "vender"
    if fam["categoria"] == "codos":
        color = re.search(r"(?i)\b(negro|rojo)s?\b", t)
        if not color:
            return None
        return pregunta_medida_faltante(f"codo {color.group(1).lower()}", None, "codos", para, usuario)
    return pregunta_medida_faltante("corrugada", None, "mangueras corrugadas", para, usuario)

def buscar_que_era_venta(nombre, args, texto):
    """'se vendió corrugada de dos': el modelo llamó `buscar` (mostró una lista) en vez de `vender`. Una afirmación de venta (sin pregunta, sin
    pedir foto) es una venta: se cambia a `vender` (la vista previa pide confirmación igual). → (nombre, args) nuevos, o None."""
    if nombre != "buscar" or "?" in (texto or "") or not _VERBO_VENTA.search(texto or "") or args.get("con_imagen") or pide_imagen(texto):
        return None
    return "vender", {"producto_query": args.get("query", "") or "", "cantidad": 1, "categoria": args.get("categoria"), "atributos": args.get("atributos")}

MEDIDA_PREGUNTADA = {}   # usuario → {"dado": "40", "falta": 0|1, "valores": [...], "ts": epoch}: lo que el bot acaba de preguntar (en memoria; vale 10 min)

def completar_medida(usuario, nombre, args, texto=""):
    """Tras '¿Qué diámetro necesitas?', el usuario contesta 'la de dos y media' y el modelo a veces se olvida del 40 que dijo antes (o lo deja en
    el texto de búsqueda). Con el estado de lo preguntado, la medida se completa SIEMPRE: [diámetro, largo] en orden. True si lo hizo."""
    est = MEDIDA_PREGUNTADA.get(usuario)
    if not est or nombre not in ("vender", "actualizar", "buscar") or time.time() - est["ts"] > 600:
        return False
    fam = FAMILIAS_MEDIDA.get(est.get("familia") or "mangueras corrugadas") or FAMILIAS_MEDIDA["mangueras corrugadas"]
    if re.search("(?i)" + fam["patron"], texto or ""):
        return False   # "mejor una corrugada de 40": nombra el producto, es un pedido NUEVO y no la respuesta a la pregunta
    campo = "query" if nombre == "buscar" else "producto_query"
    atrs = [a.strip() for a in (args.get("atributos") or [])]
    q = (args.get(campo) or "").strip()
    if len(atrs) >= 2:
        return False   # un pedido nuevo y completo ("ahora una de 2 por 26") no se pisa con lo que se había preguntado
    if (args.get("categoria") or fam["categoria"]).lower() != fam["categoria"]:
        return False   # p. ej. "se vendió 1 radiador gol" tras la pregunta: otro producto, no la respuesta
    d_txt, l_txt = medidas_en_texto(texto, fam) if len((texto or "").split()) <= 6 and (not q or re.search("(?i)" + fam["patron"] + r"|manguera|^\d|^[\d/ ]+$" + fam["extra_q"], q)) else ([], [])
    candidatos = {x for x in atrs + ([q] if q else []) + d_txt + l_txt if x in est["valores"]}
    if len(candidatos) != 1:
        return False
    m = candidatos.pop()
    completa = [m, est["dado"]] if est["falta"] == 0 else [est["dado"], m]
    if atrs == completa and q != m:
        MEDIDA_PREGUNTADA.pop(usuario, None)
        return False   # ya venía completa
    args["atributos"] = completa
    if q == m:
        args[campo] = est.get("query") or "" if fam["mezcla"] else ""
    elif not q and fam["mezcla"] and est.get("query"):
        args[campo] = est["query"]   # el color («codo negro») que se había dicho antes y el modelo soltó
    if not args.get("categoria"):
        args["categoria"] = fam["categoria"]
    MEDIDA_PREGUNTADA.pop(usuario, None)
    return True

def pregunta_medida_faltante(query, atributos, categoria, para="vender", usuario=None):
    """Corrugadas y codos negro/rojo con UN solo dato ('corrugada de 40', 'codo negro de 2 y media'): faltan el diámetro o el largo. Antes el bot mostraba
    5 de las 8 (o 16) medidas con 'hay más, afina la búsqueda' (y 'se vendió una corrugada de 40' hasta mezclaba otras). Ahora muestra TODAS las del dato
    que falta, con precio y stock, y pregunta cuál. → texto, o None si no aplica (más de un dato, otro producto, un solo resultado…)."""
    if not atributos:
        fam = familia_medida(categoria, query)
        if not fam:
            return None
        # "se vendió corrugada" / "codo negro" sin ninguna medida: antes salían 5 de las 128 (o 36) con "hay más resultados"
        todas = buscar_productos(query, categoria=categoria, limit=200, relajar_categoria=False)
        con = [(p, partes_de_medida(p)) for p in todas]
        con = [(p, x) for p, x in con if x]
        if len(con) > MAX_BUSQUEDA and len({p["product_tmpl_id"][0] for p, _ in con}) == 1 and (fam["mezcla"] or len(con) == len(todas)):
            d = sorted({x[0] for _, x in con}, key=_num_medida); l = sorted({x[1] for _, x in con}, key=_num_medida)
            largos_txt = ", ".join(l) if len(l) <= 4 else f"{l[0]} a {l[-1]}"
            texto = (f"📏 *{con[0][0]['product_tmpl_id'][1]}* se define por diámetro y largo ({len(con)} medidas).\n\n"
                     f"Diámetros: {', '.join(d)}\nLargos: {largos_txt}\n\n")
            texto += f"¿Qué medida necesitas? Por ejemplo: «{fam['ejemplo']}»." if para != "buscar" else "Dime el diámetro y el largo (o solo uno, y te muestro las opciones)."
            return texto
        return None
    if len(atributos) != 1:
        return None
    productos = buscar_productos(query, atributos=atributos, categoria=categoria, limit=40, relajar_categoria=False)
    con = [(p, partes_de_medida(p)) for p in productos]
    con = [(p, x) for p, x in con if x]   # los semicodos y demás que traiga la búsqueda por nombre no tienen «medida»: no cuentan
    if len(con) < 2 or len({p["product_tmpl_id"][0] for p, _ in con}) != 1:
        return None
    productos = [p for p, _ in con]; partes = [x for _, x in con]
    dado = atributos[0]
    if all(x[0] == dado for x in partes):
        falta = 1
    elif all(x[1] == dado for x in partes):
        falta = 0
    else:
        return None
    nombre_dado, nombre_falta = ("diámetro", "largo") if falta == 1 else ("largo", "diámetro")
    filas = sorted(zip(productos, partes), key=lambda t: _num_medida(t[1][falta]))
    lineas = []
    for p, x in filas:
        stock = int(p["qty_available"])
        lineas.append(f"• {x[falta]}: ${p['lst_price']:,.0f} | 📦 {str(stock) + ' unid.' if stock > 0 else '⚠️ sin stock'}")
    plural = "largos" if falta == 1 else "diámetros"
    texto = f"📏 *{productos[0]['product_tmpl_id'][1]}* de {nombre_dado} {dado} viene en {len(filas)} {plural}:\n\n" + "\n".join(lineas)
    if usuario:
        fam_k = "codos" if set(dict(productos[0].get("_variantes") or [])) & {"medida codo negro", "medida codo rojo"} else "mangueras corrugadas"
        MEDIDA_PREGUNTADA[usuario] = {"dado": dado, "falta": falta, "valores": [x[falta] for _, x in filas], "ts": time.time(), "familia": fam_k, "query": query}
    if para != "buscar":
        texto += f"\n\n¿Qué {nombre_falta} necesitas? (te falta decirlo)"
    return texto

_MEDIDA = r"\d{1,2}(?:\s+\d/\d)?|\d/\d"

def corregir_medida_suelta(nombre, args):
    """'se vendió una corrugada de 40': el modelo a veces manda la medida en el TEXTO de búsqueda ('40', 'corrugada de 40', 'corrugada de 2 1/2
    por 40') en vez de `atributos`. En corrugadas un número suelto es una MEDIDA, no una palabra del nombre ni un código; y si ya había otra
    medida en `atributos`, se unen (la menor es el diámetro). True si corrigió algo."""
    if nombre not in ("vender", "actualizar", "buscar"):
        return False
    campo = "query" if nombre == "buscar" else "producto_query"
    q = (args.get(campo) or "").strip()
    fam = familia_medida(args.get("categoria"), q)   # por la CATEGORÍA; 'semicodo', 'reductor' y 'Ele' (en el texto) quedan fuera
    if not fam or (args.get("categoria") or "").lower() != fam["categoria"]:
        return False
    atrs = [a.strip() for a in (args.get("atributos") or [])]
    # medidas dentro del texto, p. ej. "corrugada de 2 1/2 por 40" → ["2 1/2", "40"] y el texto queda "corrugada"
    en_texto = [m.group(0).strip() for m in re.finditer(rf"(?<![\w/.,\-])(?:{_MEDIDA})(?![\w/.,\-]|\s*[a-zA-Z]*\d)", q)] if q else []
    nuevos = [m for m in en_texto if m not in atrs]   # una medida que ya viene en `atributos` y se repite en el texto ("30" + ["2 1/2","30"]) solo sobra
    if not en_texto or len(nuevos) + len(atrs) > 2:
        return False
    if any(not re.fullmatch(_MEDIDA, a) for a in atrs):
        return False
    resto = q
    for m in en_texto:
        resto = re.sub(rf"(?i)(?:\b(?:de|por|x)\s+)?(?<![\w/.,]){re.escape(m)}(?![\w/.,])(?:\s+pulgadas?)?", " ", resto, count=1)
    resto = re.sub(r"\s+", " ", re.sub(r"(?i)\b(?:de|por|x|pulgadas?)\b\s*$", "", resto.strip())).strip()
    args[campo] = resto
    if nuevos:
        args["atributos"] = sorted(atrs + nuevos, key=_num_medida)
    return True

def handler_buscar(usuario, args):
    query = args.get("query", "")
    atributos = args.get("atributos") or None
    categoria = args.get("categoria") or None
    con_imagen = bool(args.get("con_imagen"))
    if not con_imagen:
        tabla = pregunta_medida_faltante(query, atributos, categoria, para="buscar", usuario=usuario)
        if tabla:
            return tabla
    productos = buscar_productos(query, atributos=atributos, categoria=categoria, limit=MAX_BUSQUEDA + 1,
                                 relajar_categoria=not con_imagen)
    sugerencia = sugerencia_codigo(usuario, "buscar", args, query, productos)
    if sugerencia:
        return sugerencia
    if not productos:
        oferta = "" if con_imagen else ofrecer_faltante(usuario, descripcion_busqueda(query, categoria, atributos), categoria,
                                                          query if re.fullmatch(r"[A-Za-z]{0,3}-?\d[\w-]{2,}", query or "") else None, None, "no_existe")
        return f"❌ No encontré productos para *{query or categoria}*." + oferta
    hay_mas = len(productos) > MAX_BUSQUEDA
    productos = productos[:MAX_BUSQUEDA]
    if con_imagen and args.get("_texto"):
        productos = filtrar_por_distintivos(args["_texto"], productos)

    guardar_pendiente(usuario, {
        "tipo": "lista_productos",
        "opciones": [
            {
                "id": p["id"],
                "nombre": p["display_name"],
                "stock": int(p["qty_available"]),
                "precio": p["lst_price"],
                "codigo": p.get("default_code", ""),
                "template_id": p["product_tmpl_id"][0] if p.get("product_tmpl_id") else None,
            }
            for p in productos
        ],
        "ultima_busqueda_ids": [p["id"] for p in productos],
    })

    if con_imagen:
        return preparar_envio_imagenes(usuario, productos, query, hay_mas)
    texto = formatear_lista_productos(productos, query)
    if hay_mas:
        texto += f"\n\n{MENSAJE_HAY_MAS}"
    if all(not p.get("_exacta", True) for p in productos):
        # el buscador tuvo que relajarse (ninguno tiene todas las palabras): eso es «lo más parecido», no lo que pidieron
        return texto + ofrecer_faltante(usuario, descripcion_busqueda(query, categoria, atributos), categoria, None, None, "no_existe", "No es exactamente lo que buscas. ")
    if len(productos) == 1 and not hay_mas and int(productos[0]["qty_available"]) <= 0:
        # un producto concreto, sin existencias: se ofrece anotarlo para pedírselo al proveedor
        p = productos[0]
        return texto + ofrecer_faltante(usuario, p["display_name"], categoria, p.get("default_code") or None, p["id"], "sin_stock", "Está sin stock. ")
    return texto

def handler_vender(usuario, args):
    query = args.get("producto_query", "") or ""
    cantidad = int(args.get("cantidad", 1))
    categoria = args.get("categoria") or None
    atributos = args.get("atributos") or None
    opcion = args.get("opcion_lista")

    # Si referencia una opción de la lista anterior por número, usarla directamente
    if opcion:
        pendiente = obtener_pendiente(usuario)
        opciones = []
        if pendiente:
            if pendiente.get("tipo") == "lista_productos":
                opciones = pendiente.get("opciones", [])
            elif pendiente.get("tipo") == "seleccion_venta":
                opciones = pendiente.get("opciones", [])
        if opciones and 1 <= opcion <= len(opciones):
            sel = opciones[opcion - 1]
            agregar_venta_pendiente(usuario, {
                "product_id": sel["id"],
                "product_name": sel["nombre"],
                "stock_actual": sel["stock"],
                "cantidad": cantidad,
                "precio": sel.get("precio"),
            })
            return "__BATCH_VENTA__"

    # Si la búsqueda anterior encontró exactamente 1 producto, usarlo directamente
    pendiente_actual = obtener_pendiente(usuario)
    if pendiente_actual and pendiente_actual.get("tipo") == "lista_productos":
        opciones_previas = pendiente_actual.get("opciones", [])
        if len(opciones_previas) == 1 and not query_es_otro_codigo(query, opciones_previas[0]):
            sel = opciones_previas[0]
            agregar_venta_pendiente(usuario, {
                "product_id": sel["id"],
                "product_name": sel["nombre"],
                "stock_actual": sel["stock"],
                "cantidad": cantidad,
                "precio": sel.get("precio"),
            })
            return "__BATCH_VENTA__"

    if opcion and not query and not categoria and not atributos:
        return mensaje_opcion_sin_lista(usuario, opcion)
    if not query and not categoria and not atributos and not opcion:
        return "❌ No me dijiste qué producto vender."
    pregunta = pregunta_medida_faltante(query, atributos, categoria, para="vender", usuario=usuario)
    if pregunta:
        return pregunta

    productos = buscar_productos(query, atributos=atributos, categoria=categoria)
    sugerencia = sugerencia_codigo(usuario, "vender", args, query, productos)
    if sugerencia:
        return sugerencia
    if len(productos) > 1:
        productos = filtrar_por_busqueda_anterior(usuario, productos)

    if not productos:
        return f"❌ No encontré productos para *{query}*."

    if len(productos) == 1:
        p = productos[0]
        agregar_venta_pendiente(usuario, {
            "product_id": p["id"],
            "product_name": p["display_name"],
            "stock_actual": int(p["qty_available"]),
            "cantidad": cantidad,
            "precio": p["lst_price"],
        })
        return "__BATCH_VENTA__"

    # Múltiples resultados
    guardar_pendiente(usuario, {
        "tipo": "seleccion_venta",
        "cantidad": cantidad,
        "opciones": [
            {"id": p["id"], "nombre": p["display_name"], "stock": int(p["qty_available"]),
             "precio": p["lst_price"], "codigo": p.get("default_code", "")}
            for p in productos
        ],
    })
    respuesta = f"Encontré varios productos ({cantidad} unidad/es):\n\n"
    for i, p in enumerate(productos, 1):
        stock = int(p["qty_available"])
        codigo = p.get("default_code", "")
        respuesta += f"{i}. *{p['display_name']}*"
        respuesta += f"\n   💰 ${p['lst_price']:,.0f} | 📦 Stock: {stock}\n"
    respuesta += "\n¿Cuál se vendió? Dime el número."
    return respuesta.strip()

def codigo_corto(nombre):
    """'[5490] Manguera radiador…' → '[5490]'; sin código entre corchetes, el comienzo del nombre."""
    m = re.match(r"\s*(\[[^\]]+\])", nombre or "")
    return m.group(1) if m else (nombre or "")[:30]

def agregar_venta_pendiente(usuario, nueva):
    """Agrega una venta al lote pendiente de confirmar. Si ESE producto ya está en el lote, la nueva cantidad lo REEMPLAZA en vez de
    sumar otra venta: 'se vendió la 5490' + 'perdón, fueron 2' es una corrección (antes quedaban 2 ventas pendientes: 1 + 2 = 3 unidades).
    El aviso de lo que se corrigió se guarda en el pendiente y lo muestra `vista_previa_venta`."""
    pend = obtener_pendiente(usuario)
    if not (pend and pend.get("tipo") == "venta"):
        pend = {"tipo": "venta", "ventas": []}
    ventas = pend.setdefault("ventas", [])
    for i, v in enumerate(ventas):
        if v["product_id"] == nueva["product_id"]:
            if v["cantidad"] != nueva["cantidad"]:
                msg = f"✏️ {codigo_corto(nueva['product_name'])} ya estaba en la lista: la cantidad pasó de {v['cantidad']} a {nueva['cantidad']}."
            else:
                msg = f"✏️ {codigo_corto(nueva['product_name'])} ya estaba en la lista (×{v['cantidad']}); no la repetí."
            pend["aviso"] = f"{pend['aviso']}\n{msg}" if pend.get("aviso") else msg
            ventas[i] = nueva
            break
    else:
        ventas.append(nueva)
    guardar_pendiente(usuario, pend)
    return ventas

def vista_previa_venta(usuario):
    """Texto del lote de ventas pendiente (con el aviso de corrección, una sola vez). None si no hay ventas pendientes."""
    pend = obtener_pendiente(usuario)
    if not (pend and pend.get("tipo") == "venta" and pend.get("ventas")):
        return None
    aviso = pend.pop("aviso", None)
    if aviso:
        guardar_pendiente(usuario, pend)
    texto = formatear_ventas_pendientes(pend["ventas"])
    return f"{aviso}\n\n{texto}" if aviso else texto

def linea_venta(v):
    """'💰 $45.000 | 📦 Stock: 6 → 5'; con cantidad > 1: '💰 2 × $45.000 = $90.000 | 📦 Stock: 6 → 4'."""
    cant = v["cantidad"]
    stock_txt = f"📦 Stock: {v['stock_actual']} → {v['stock_actual'] - cant}"
    precio = v.get("precio")
    if precio is None:
        return stock_txt
    precio_txt = f"💰 {cant} × ${precio:,.0f} = ${precio * cant:,.0f}" if cant > 1 else f"💰 ${precio:,.0f}"
    return f"{precio_txt} | {stock_txt}"

def formatear_ventas_pendientes(ventas):
    if len(ventas) == 1:
        v = ventas[0]
        aviso = "\n⚠️ Stock insuficiente" if v["stock_actual"] < v["cantidad"] else ""
        return (
            f"📦 *{v['product_name']}*\n"
            f"{linea_venta(v)}{aviso}\n\n"
            f"¿Confirmas?"
        )
    total = sum(v["cantidad"] for v in ventas)
    respuesta = f"🛒 *{len(ventas)} ventas pendientes* ({total} unidades):\n\n"
    for i, v in enumerate(ventas, 1):
        aviso = " ⚠️" if v["stock_actual"] < v["cantidad"] else ""
        respuesta += f"{i}. {v['product_name']}\n"
        respuesta += f"   {linea_venta(v)}{aviso}\n"
    if all(v.get("precio") is not None for v in ventas):
        respuesta += f"\n💵 Total: ${sum(v['precio'] * v['cantidad'] for v in ventas):,.0f}"
    respuesta += "\n\n¿Confirmas todas?"
    return respuesta

def ejecutar_venta(usuario, pendiente):
    ventas = pendiente.get("ventas", [])
    if not ventas:
        return "No hay ventas pendientes."

    resultados = []
    for v in ventas:
        try:
            orden_id = registrar_venta_completa(v["product_id"], v["cantidad"])
            registrar_venta_en_log(
                usuario, v["product_id"], v["product_name"],
                v["cantidad"], orden_id, v["stock_actual"]
            )
            nuevo_stock = v["stock_actual"] - v["cantidad"]
            resultados.append(
                f"✅ {v['product_name']}: -{v['cantidad']} → {nuevo_stock} (Orden #{orden_id})"
            )
        except Exception as e:
            log.error(f"Error en venta: {e}")
            resultados.append(f"❌ {v['product_name']}: {e}")

    limpiar_pendiente(usuario)
    return "\n".join(resultados)

# Palabra que dice el usuario → claves de `cambios` que le corresponden ("stock" cubre sumar y dejar exacto)
_CAMPOS_CAMBIO = {
    "stock": ("establecer_stock", "agregar_stock"), "inventario": ("establecer_stock", "agregar_stock"),
    "precio": ("nuevo_precio",), "costo": ("nuevo_costo",), "nombre": ("nuevo_nombre",),
    "codigo": ("nuevo_codigo",), "código": ("nuevo_codigo",), "referencia": ("nuevo_codigo",),
}
_GRUPO_CAMBIO = {"establecer_stock": "stock", "agregar_stock": "stock", "nuevo_precio": "precio", "nuevo_costo": "costo", "nuevo_nombre": "nombre", "nuevo_codigo": "código"}

def descripcion_cambio(clave, valor):
    return {"nuevo_precio": f"precio ${valor:,.0f}" if isinstance(valor, (int, float)) else f"precio {valor}",
            "nuevo_costo": f"costo ${valor:,.0f}" if isinstance(valor, (int, float)) else f"costo {valor}",
            "establecer_stock": f"stock = {valor}", "agregar_stock": f"stock +{valor}",
            "nuevo_nombre": f"nombre «{valor}»", "nuevo_codigo": f"código {valor}"}[clave]

def agregar_actualizacion_pendiente(usuario, act):
    """Suma una actualización al lote pendiente. Si ESE producto ya estaba, sus cambios se MEZCLAN: lo nuevo reemplaza el mismo campo
    ('el stock a 1000' + 'el stock a 1' → stock = 1) y los demás campos se conservan. Antes cada llamada era una entrada nueva
    y quedaban 5 'actualizaciones' del mismo producto. `act` = {product_id, product_name, cambios, info:{stock,precio,codigo,nombre}}.
    Los campos reemplazados se avisan una vez en la vista previa (`aviso` del pendiente)."""
    error = validar_cambios(act["product_id"], act["cambios"])
    if error:
        return error   # no se agrega: no se le pide confirmar algo que Odoo no puede hacer como se pidió
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") == "actualizacion":   # formato viejo (un solo producto)
        pend = {"tipo": "actualizacion_batch", "actualizaciones": [
            {"product_id": pend["product_id"], "product_name": pend["product_name"], "cambios": pend["cambios"]}]}
    if not (pend and pend.get("tipo") == "actualizacion_batch"):
        pend = {"tipo": "actualizacion_batch", "actualizaciones": []}
    acts = pend.setdefault("actualizaciones", [])
    for a in acts:
        if a["product_id"] != act["product_id"]:
            continue
        for k, v in act["cambios"].items():
            grupo = _GRUPO_CAMBIO[k]
            for k_viejo in [x for x in a["cambios"] if _GRUPO_CAMBIO[x] == grupo]:   # sumar y dejar exacto no pueden ir juntos
                if (k_viejo, a["cambios"][k_viejo]) != (k, v):
                    msg = f"✏️ {codigo_corto(a['product_name'])}: {descripcion_cambio(k_viejo, a['cambios'][k_viejo])} → {descripcion_cambio(k, v)}."
                    pend["aviso"] = f"{pend['aviso']}\n{msg}" if pend.get("aviso") else msg
                del a["cambios"][k_viejo]
            a["cambios"][k] = v
        if act.get("info"):
            a["info"] = act["info"]
        break
    else:
        acts.append(act)
    guardar_pendiente(usuario, pend)
    return None

def info_actual(a):
    """Datos actuales del producto de una actualización (se guardan al agregarla; si faltan, se leen de Odoo)."""
    if a.get("info"):
        info = a["info"]
        if "nuevo_costo" in a["cambios"] and "costo" not in info:
            info = {**info, "costo": costo_actual(a["product_id"])}
        if ("nuevo_precio" in a["cambios"] or "nuevo_nombre" in a["cambios"]) and "variantes" not in info:
            iv = info_variante(a["product_id"])
            info = {**info, "variantes": {"n": iv["n"], "plantilla": iv["plantilla"]} if iv and iv["es_variante"] else None}
        return info
    p = odoo_search_read("product.product", [["id", "=", a["product_id"]]], ["display_name", "lst_price", "qty_available", "default_code"], limit=1)
    p = p[0] if p else {}
    info = {"nombre": a["product_name"], "stock": int(p.get("qty_available", 0)), "precio": p.get("lst_price", 0), "codigo": p.get("default_code", "")}
    if "nuevo_costo" in a["cambios"]:
        info["costo"] = costo_actual(a["product_id"])
    if "nuevo_precio" in a["cambios"] or "nuevo_nombre" in a["cambios"]:
        iv = info_variante(a["product_id"])
        info["variantes"] = {"n": iv["n"], "plantilla": iv["plantilla"]} if iv and iv["es_variante"] else None
    return info

def formatear_actualizaciones(acts):
    if len(acts) == 1:
        a = acts[0]
        return f"📋 *{a['product_name']}*\n\n{generar_preview_cambios(info_actual(a), a['cambios'])}\n\n¿Confirmas?"
    texto = f"📋 *{len(acts)} productos por actualizar*\n\n"
    for i, a in enumerate(acts, 1):
        texto += f"{i}. *{a['product_name']}*\n   " + generar_preview_cambios(info_actual(a), a["cambios"]).replace("\n", "\n   ") + "\n\n"
    return texto + "¿Confirmas todas?"

def vista_actualizaciones(usuario):
    """Texto del lote de actualizaciones pendiente, con los avisos de campos reemplazados (una sola vez). None si no hay."""
    pend = obtener_pendiente(usuario)
    if not (pend and pend.get("tipo") == "actualizacion_batch" and pend.get("actualizaciones")):
        return None
    aviso = pend.pop("aviso", None)
    if aviso:
        guardar_pendiente(usuario, pend)
    texto = formatear_actualizaciones(pend["actualizaciones"])
    return f"{aviso}\n\n{texto}" if aviso else texto

_VERBO_QUITAR = re.compile(r"^(?:cancel|cancél|quit|quít|elimin|borr|olv[ií]d|cambies|actualices|modifiques|toques|ajustes|dejes)\w*$")
_RELLENO_QUITAR = {"las", "los", "la", "el", "lo", "de", "del", "y", "e", "solo", "sólo", "por", "favor", "actualizaciones", "actualización",
                   "actualizacion", "cambios", "cambio", "modificaciones", "modificación", "modificacion", "pendientes", "pendiente", "ya", "no", "que", "se", "al", "también"}

def atajo_quitar_cambio(usuario, texto):
    """'Cancela las actualizaciones de stock' / 'quita el precio' / 'no cambies el stock', con una actualización pendiente:
    saca SOLO ese campo del pendiente (los demás cambios se quedan). Antes el modelo lo tomaba por otra actualización y la sumaba."""
    pend = obtener_pendiente(usuario)
    if not pend or pend.get("tipo") not in ("actualizacion", "actualizacion_batch"):
        return None
    toks = re.findall(r"[a-záéíóúüñ]+", (texto or "").lower())
    if re.search(r"\d", texto or "") or not toks or len(toks) > 9:
        return None
    if not (_VERBO_QUITAR.match(toks[0]) or (toks[0] == "no" and len(toks) > 1 and _VERBO_QUITAR.match(toks[1]))):
        return None
    resto = [t for t in toks if not _VERBO_QUITAR.match(t) and t not in _RELLENO_QUITAR]
    if not resto or any(t not in _CAMPOS_CAMBIO for t in resto):
        return None
    claves = {k for t in resto for k in _CAMPOS_CAMBIO[t]}
    if pend["tipo"] == "actualizacion":
        pend = {"tipo": "actualizacion_batch", "actualizaciones": [
            {"product_id": pend["product_id"], "product_name": pend["product_name"], "cambios": pend["cambios"]}]}
    quitados = sorted({_GRUPO_CAMBIO[k] for a in pend["actualizaciones"] for k in a["cambios"] if k in claves})
    if not quitados:
        return f"No hay ningún cambio de {' ni de '.join(sorted(set(resto)))} pendiente.\n\n{formatear_actualizaciones(pend['actualizaciones'])}"
    for a in pend["actualizaciones"]:
        a["cambios"] = {k: v for k, v in a["cambios"].items() if k not in claves}
    pend["actualizaciones"] = [a for a in pend["actualizaciones"] if a["cambios"]]
    pend.pop("aviso", None)
    quite = f"Quité el cambio de {' y de '.join(quitados)}"
    if not pend["actualizaciones"]:
        limpiar_pendiente(usuario)
        return f"❌ {quite}. No queda nada pendiente."
    guardar_pendiente(usuario, pend)
    return f"✂️ {quite}.\n\n{formatear_actualizaciones(pend['actualizaciones'])}"

def ejecutar_actualizacion(usuario, pendiente):
    actualizaciones = pendiente.get("actualizaciones", [])
    if not actualizaciones:
        actualizaciones = [pendiente]   # formato antiguo (un solo producto)
    limpiar_pendiente(usuario)   # primero: si algo falla a medias, un "sí" repetido no vuelve a sumar stock ya sumado
    resultados = []
    for act in actualizaciones:
        try:
            cambios = act.get("cambios", {})
            actualizar_producto(act["product_id"], **cambios)
            stock_final = get_stock_actual(act["product_id"])
            partes = []
            if "nuevo_precio" in cambios:
                partes.append(f"Precio: ${cambios['nuevo_precio']:,.0f}")
            if "nuevo_costo" in cambios:
                partes.append(f"Costo: ${cambios['nuevo_costo']:,.0f}")
            if cambios.get("nuevo_nombre"):
                partes.append(f"Nombre: {cambios['nuevo_nombre']}")
            if cambios.get("nuevo_codigo"):
                partes.append(f"Código: {cambios['nuevo_codigo']}")
            partes.append(f"Stock: {stock_final}")
            linea = f"✅ *{act['product_name']}* → " + " | ".join(partes)
            if stock_final > 0 and ("agregar_stock" in cambios or "establecer_stock" in cambios):
                try:
                    n_ped = cerrar_faltantes_producto(act["product_id"])
                    if n_ped:
                        linea += f"\n📋 Estaba en la lista de pedidos ({n_ped} pedido{'s' if n_ped != 1 else ''}): ya lo quité de la lista."
                except Exception as e:
                    log.warning(f"No pude cerrar la lista de pedidos de {act.get('product_name')}: {e}")
            resultados.append(linea)
        except Exception as e:
            log.error(f"Error en actualización de {act.get('product_name')}: {e}")
            log.error(traceback.format_exc())
            resultados.append(f"❌ *{act.get('product_name')}*: {e}\nRevísalo en Odoo antes de repetirlo.")
    return "\n".join(resultados)

def ejecutar_creacion(usuario, pendiente):
    try:
        crear_producto(
            pendiente["nombre"], pendiente["precio"], pendiente["cantidad"],
            pendiente["categoria_id"], pendiente.get("referencia"), pendiente.get("costo"),
        )
        limpiar_pendiente(usuario)
        costo = pendiente.get("costo")
        return (
            f"✅ Producto creado\n"
            f"*{pendiente['nombre']}*\n"
            f"Precio: ${pendiente['precio']:,.0f} COP\n"
            + (f"Costo: ${costo:,.0f} COP\n" if costo is not None else "")
            + f"Stock: {pendiente['cantidad']}"
        )
    except Exception as e:
        log.error(f"Error creando producto: {e}")
        return f"❌ Error: {e}"

def handler_confirmar(usuario, args):
    pendiente = obtener_pendiente(usuario)
    if not pendiente:
        return "No hay nada pendiente por confirmar."

    # Cancelar acción pendiente (sin tocar Odoo)
    if args.get("cancelar"):
        limpiar_pendiente(usuario)
        return "❌ Acción cancelada."

    opcion = args.get("opcion")
    tipo = pendiente.get("tipo")

    # Selección de número desde lista de venta
    if tipo == "seleccion_venta":
        if not opcion:
            return "¿Cuál opción? Dime el número."
        opciones = pendiente.get("opciones", [])
        if not (1 <= opcion <= len(opciones)):
            return f"Opción inválida (1 a {len(opciones)})."
        sel = opciones[opcion - 1]
        cantidad = pendiente.get("cantidad", 1)
        agregar_venta_pendiente(usuario, {
            "product_id": sel["id"],
            "product_name": sel["nombre"],
            "stock_actual": sel["stock"],
            "cantidad": cantidad,
            "precio": sel.get("precio"),
        })
        return vista_previa_venta(usuario)

    # Selección desde lista de actualización
    if tipo == "seleccion_actualizacion":
        if not opcion:
            return "¿Cuál opción? Dime el número."
        opciones = pendiente.get("opciones", [])
        if not (1 <= opcion <= len(opciones)):
            return f"Opción inválida (1 a {len(opciones)})."
        sel = opciones[opcion - 1]
        error = agregar_actualizacion_pendiente(usuario, {
            "product_id": sel["id"], "product_name": sel["nombre"], "cambios": pendiente.get("cambios", {}), "info": sel,
        })
        return error or vista_actualizaciones(usuario)

    # Selección desde lista de búsqueda (mostrar detalle)
    if tipo == "lista_productos":
        if not opcion:
            return "¿Cuál opción? Dime el número."
        opciones = pendiente.get("opciones", [])
        if not (1 <= opcion <= len(opciones)):
            return f"Opción inválida (1 a {len(opciones)})."
        sel = opciones[opcion - 1]
        limpiar_pendiente(usuario)
        return (
            f"📦 *{sel['nombre']}*\n"
            f"Stock: {sel['stock']}\n"
            f"Precio: ${sel['precio']:,.0f} COP"
        )

    # Devolución: elegir de cuál venta, y confirmar
    if tipo == "seleccion_devolucion":
        if not opcion:
            return "¿De cuál venta? Dime el número."
        opciones = pendiente.get("opciones", [])
        if not (1 <= opcion <= len(opciones)):
            return f"Opción inválida (1 a {len(opciones)})."
        items, err = poner_en_lote(pendiente.get("items", []), opciones[opcion - 1], pendiente.get("cantidad"))
        if err:
            return err   # la pregunta sigue pendiente: puede elegir otra venta
        # Si había más productos en el mismo mensaje, se sigue con el siguiente; si no, queda el lote por confirmar
        avisos, pregunta, items = avanzar_devoluciones(usuario, items, pendiente.get("cola", []))
        return "\n".join(avisos + [pregunta or formatear_devoluciones(items)])
    if tipo == "devolucion":
        return ejecutar_devolucion(usuario, pendiente)

    # Confirmaciones directas
    if tipo == "venta":
        return ejecutar_venta(usuario, pendiente)
    if tipo in ("actualizacion", "actualizacion_batch"):
        return ejecutar_actualizacion(usuario, pendiente)
    if tipo == "creacion":
        return ejecutar_creacion(usuario, pendiente)

    return "No reconozco la acción pendiente."

def generar_preview_cambios(producto_info, cambios):
    """Genera texto descriptivo de los cambios a aplicar."""
    lineas = []
    var = producto_info.get("variantes")   # {"n", "plantilla"} si es una de varias variantes de la misma plantilla
    if "nuevo_nombre" in cambios:
        lineas.append(f"Nombre: {producto_info['nombre']} → {cambios['nuevo_nombre']}")
        if var:
            lineas.append(f"⚠️ El nombre es de *{var['plantilla']}* y lo comparten sus {var['n']} variantes: cambia en TODAS (no se puede por una sola).")
    if "nuevo_precio" in cambios:
        lineas.append(f"Precio: ${producto_info['precio']:,.0f} → ${cambios['nuevo_precio']:,.0f}" + (f" (solo esta variante; las otras {var['n'] - 1} no cambian)" if var else ""))
    if "nuevo_costo" in cambios:
        actual = producto_info.get("costo")
        lineas.append(f"Costo: ${actual:,.0f} → ${cambios['nuevo_costo']:,.0f}" if actual is not None else f"Costo: → ${cambios['nuevo_costo']:,.0f}")
        precio_venta = cambios.get("nuevo_precio", producto_info.get("precio"))
        if precio_venta is not None and cambios["nuevo_costo"] > precio_venta:
            lineas.append(f"⚠️ El costo supera el precio de venta (${precio_venta:,.0f}): ¿es correcto?")
    if "nuevo_codigo" in cambios:
        lineas.append(f"Código: {producto_info.get('codigo', 'N/A')} → {cambios['nuevo_codigo']}")
    if "agregar_stock" in cambios:
        nuevo = producto_info["stock"] + cambios["agregar_stock"]
        lineas.append(f"Stock: {producto_info['stock']} + {cambios['agregar_stock']} = {nuevo}")
    if "establecer_stock" in cambios:
        lineas.append(f"Stock: {producto_info['stock']} → {cambios['establecer_stock']}")
    return "\n".join(lineas)

def handler_crear(usuario, args):
    categoria_nombre = args["categoria"].lower().strip()
    categoria_id = CATEGORIAS.get(categoria_nombre)
    if not categoria_id:
        return (
            f"❌ Categoría *{args['categoria']}* no válida.\n"
            f"Categorías: {', '.join(CATEGORIAS.keys())}"
        )

    similares = producto_existe_similar(args["nombre"])
    aviso_similar = ""
    if similares:
        aviso_similar = "\n\n⚠️ Productos con nombre similar:\n"
        for s in similares[:3]:
            aviso_similar += f"• {s['name']}\n"

    costo = args.get("costo")
    if costo is not None and costo < 0:
        return "❌ El costo no puede ser negativo."
    aviso_costo = ""
    if costo is not None and costo > args["precio"]:
        aviso_costo = "\n\n⚠️ El costo es mayor que el precio de venta. Revisa los valores antes de confirmar."

    guardar_pendiente(usuario, {
        "tipo": "creacion",
        "nombre": args["nombre"],
        "precio": args["precio"],
        "costo": costo,
        "cantidad": args["cantidad"],
        "categoria_id": categoria_id,
        "categoria": args["categoria"],
        "referencia": args.get("referencia"),
    })

    return (
        f"📋 *Crear producto*\n"
        f"Nombre: {args['nombre']}\n"
        f"Precio: ${args['precio']:,.0f} COP\n"
        + (f"Costo: ${costo:,.0f} COP\n" if costo is not None else "")
        + f"Stock inicial: {args['cantidad']}\n"
        f"Categoría: {args['categoria']}"
        + (f"\nReferencia: {args.get('referencia')}" if args.get("referencia") else "")
        + aviso_costo
        + aviso_similar
        + "\n\n¿Confirmas?"
    )

def filtrar_por_busqueda_anterior(usuario, productos):
    """Si hay una búsqueda anterior guardada, filtra los resultados a solo esos productos."""
    pendiente = obtener_pendiente(usuario)
    if not pendiente:
        return productos
    ids_anteriores = set(pendiente.get("ultima_busqueda_ids", []))
    if not ids_anteriores:
        # También revisar opciones de lista_productos
        if pendiente.get("tipo") == "lista_productos":
            ids_anteriores = {opc["id"] for opc in pendiente.get("opciones", [])}
    if not ids_anteriores:
        return productos
    filtrados = [p for p in productos if p["id"] in ids_anteriores]
    return filtrados if filtrados else productos

def cambios_desde_args(args):
    """Argumentos de `actualizar` → ({cambios}, error o None). Precio = lo que paga el cliente; costo = lo que pagó la tienda."""
    cambios = {}
    if args.get("nuevo_nombre"):
        cambios["nuevo_nombre"] = args["nuevo_nombre"]
    if args.get("nuevo_precio") is not None:
        cambios["nuevo_precio"] = args["nuevo_precio"]
    if args.get("nuevo_costo") is not None:
        cambios["nuevo_costo"] = args["nuevo_costo"]
    if args.get("nuevo_codigo"):
        cambios["nuevo_codigo"] = args["nuevo_codigo"]
    if args.get("agregar_stock") is not None:
        cambios["agregar_stock"] = args["agregar_stock"]
    if args.get("establecer_stock") is not None:
        cambios["establecer_stock"] = args["establecer_stock"]
    if cambios.get("nuevo_costo") is not None and cambios["nuevo_costo"] < 0:
        return {}, "❌ El costo no puede ser negativo."
    if cambios.get("nuevo_precio") is not None and cambios["nuevo_precio"] < 0:
        return {}, "❌ El precio no puede ser negativo."
    return cambios, None

def handler_actualizar(usuario, args):
    query = args.get("producto_query", "")
    categoria = args.get("categoria") or None
    atributos = args.get("atributos") or None
    opcion = args.get("opcion_lista")

    # Si referencia una opción de la lista anterior, usarla directamente
    if opcion:
        pendiente = obtener_pendiente(usuario)
        opciones_disp = []
        if pendiente:
            for tipo_lista in ("lista_productos", "seleccion_actualizacion"):
                if pendiente.get("tipo") == tipo_lista:
                    opciones_disp = pendiente.get("opciones", [])
                    break
        if opciones_disp and 1 <= opcion <= len(opciones_disp):
            opciones = opciones_disp
            if True:
                sel = opciones[opcion - 1]
                cambios, error = cambios_desde_args(args)
                if error:
                    return error
                if not cambios:
                    return "No me dijiste qué actualizar."
                error = agregar_actualizacion_pendiente(usuario, {
                    "product_id": sel["id"], "product_name": sel["nombre"], "cambios": cambios, "info": sel,
                })
                return error or "__BATCH_ACTUALIZACION__"

    # Si la búsqueda anterior encontró exactamente 1 producto, usarlo directamente
    if not opcion:
        pendiente_actual = obtener_pendiente(usuario)
        if pendiente_actual and pendiente_actual.get("tipo") == "lista_productos":
            opciones_previas = pendiente_actual.get("opciones", [])
            if len(opciones_previas) == 1:
                opcion = 1
                if not query and not categoria and not atributos:
                    # «actualiza el stock a 3» sin decir el producto, tras una búsqueda que encontró UNO: es ese (antes: «No encontré: **»)
                    sel = opciones_previas[0]
                    cambios, error = cambios_desde_args(args)
                    if error:
                        return error
                    if not cambios:
                        return "No me dijiste qué actualizar. ¿Nombre, precio, costo, código o stock?"
                    error = agregar_actualizacion_pendiente(usuario, {
                        "product_id": sel["id"], "product_name": sel["nombre"], "cambios": cambios, "info": sel,
                    })
                    return error or "__BATCH_ACTUALIZACION__"

    if opcion and not query and not categoria and not atributos:
        return mensaje_opcion_sin_lista(usuario, opcion)
    if not query and not categoria and not atributos and not opcion:
        return "❌ No me dijiste qué producto actualizar."
    pregunta = pregunta_medida_faltante(query, atributos, categoria, para="actualizar", usuario=usuario)
    if pregunta:
        return pregunta

    productos = buscar_productos(query, atributos=atributos, categoria=categoria)
    if len(productos) > 1:
        productos = filtrar_por_busqueda_anterior(usuario, productos)

    if not productos:
        return f"❌ No encontré: *{query}*"

    cambios, error = cambios_desde_args(args)
    if error:
        return error
    if not cambios:
        return "No me dijiste qué actualizar. ¿Nombre, precio, costo, código o stock?"

    if len(productos) > 1:
        guardar_pendiente(usuario, {
            "tipo": "seleccion_actualizacion",
            "cambios": cambios,
            "opciones": [
                {"id": p["id"], "nombre": p["display_name"], "stock": int(p["qty_available"]),
                 "precio": p["lst_price"], "codigo": p.get("default_code", "")}
                for p in productos
            ],
        })
        respuesta = "Encontré varios productos. ¿Cuál actualizo?\n\n"
        for i, p in enumerate(productos, 1):
            stock = int(p["qty_available"])
            codigo = p.get("default_code", "")
            respuesta += f"{i}. *{p['display_name']}*"
            respuesta += f"\n   📦 Stock: {stock} | 💰 ${p['lst_price']:,.0f}\n"
        return respuesta.strip()

    p = productos[0]
    producto_info = {
        "id": p["id"],
        "nombre": p["display_name"],
        "stock": int(p["qty_available"]),
        "precio": p["lst_price"],
        "codigo": p.get("default_code", ""),
    }
    error = agregar_actualizacion_pendiente(usuario, {
        "product_id": p["id"], "product_name": p["display_name"], "cambios": cambios, "info": producto_info,
    })
    return error or "__BATCH_ACTUALIZACION__"

def handler_sin_stock(usuario, args):
    """«Vinieron por X y no había»: lo dijo él con todas las letras, se anota directo (sin preguntar)."""
    nombre = (args.get("nombre") or "").strip()
    if not nombre:
        return "¿Qué producto pidió el cliente?"
    OFERTA_FALTANTE.pop(usuario, None)   # si una búsqueda acababa de ofrecerlo, ya no hace falta
    unidades = args.get("unidades", 1) or 1
    veces, dup = anotar_faltante(nombre, args.get("categoria"), args.get("codigo"), unidades, None, "explicito")
    return texto_anotado(nombre, veces, dup, unidades)

def handler_reporte_sin_stock(usuario, args):
    return texto_lista_pedidos(usuario, args.get("periodo"))

def handler_deshacer(usuario, args):
    venta = ultima_venta_del_usuario(usuario)
    if not venta:
        return "No tienes ventas recientes para deshacer."
    restante = venta["cantidad"] - venta.get("devuelto", 0)
    try:
        # Antes se ponía el stock en el valor que había ANTES de la venta (stock_anterior): si después hubo otra venta o un ingreso
        # de ese producto, se pisaban. Ahora se suman las unidades al stock actual, y solo si Odoo confirma que canceló la orden.
        r = aplicar_devolucion(venta["orden_id"], venta["product_id"], restante)
        if not r["ok"]:
            return f"❌ No pude deshacer la venta: {r['msg']}\nNo moví el stock."
        if r["error_stock"]:
            return (f"⚠️ La orden ya se actualizó ({r['msg']}) pero NO pude sumar el stock: {r['error_stock']}\n"
                    f"Súmale {restante} al stock de *{venta['product_name']}* a mano.")
        return (
            f"✅ Venta deshecha\n"
            f"*{venta['product_name']}*\n"
            f"{r['msg']}\n"
            f"📦 Stock: {r['antes']} → {r['despues']}"
            + (f"\n{r['aviso']}" if r["aviso"] else "")
        )
    except Exception as e:
        log.error(f"Error deshaciendo venta: {e}")
        return f"❌ Error al deshacer: {e}"

def precio_venta_txt(n, precio):
    """Cantidad × precio por unidad: '2 × $30,000 = $60,000' / '1 × $30,000'. (Antes '×2 a $30,000' se leía como 2 por $30,000.)"""
    if precio is None:
        return f"×{n}"
    return f"{n} × ${precio:,.0f} = ${precio * n:,.0f}" if n > 1 else f"1 × ${precio:,.0f}"

def poner_en_lote(items, op, cantidad):
    """Agrega al lote de devoluciones la venta `op` con `cantidad` unidades (por defecto 1). Si esa venta ya estaba en el lote, REEMPLAZA
    su cantidad ('no, eran las dos'). → (items nuevos, error o None); con error el lote queda igual."""
    restante = op["restante"]
    cantidad = cantidad or 1
    if cantidad > restante:
        return items, (f"Esa venta ({op['orden_nombre']}, {op['fecha']}) tiene {restante} unidad(es) sin devolver; "
                       f"no se pueden devolver {cantidad}.")
    item = {k: op.get(k) for k in ("orden_id", "orden_nombre", "product_id", "product_name", "fecha", "restante", "precio")}
    item["cantidad"] = cantidad
    item["stock"] = get_stock_actual(variante_vigente(op["product_id"]))
    nuevos = [item if it["orden_id"] == item["orden_id"] else it for it in items]
    if not any(it["orden_id"] == item["orden_id"] for it in items):
        nuevos.append(item)
    return nuevos, None

def bloque_devolucion(it, stock_antes):
    """Qué pasará con UNA venta: las líneas de la vista previa (sin título ni pregunta)."""
    restante, cantidad, precio = it["restante"], it["cantidad"], it.get("precio")
    if cantidad == restante:
        efecto = "se CANCELA la orden"
    else:
        efecto = f"la orden pasa de ×{restante} a ×{restante - cantidad}"
        if precio is not None:
            efecto += f" (${precio * restante:,.0f} → ${precio * (restante - cantidad):,.0f})"
    return (f"Venta {it['orden_nombre']} — {it['fecha']} — {precio_venta_txt(restante, precio)}\n"
            f"Devuelve {cantidad} de {restante}: {efecto}\n"
            f"📦 Stock: {stock_antes} → {stock_antes + cantidad}")

def formatear_devoluciones(items):
    """Vista previa del lote de devoluciones pendiente. El stock de dos ventas del MISMO producto se acumula (6 → 7 → 8)."""
    sumado = {}
    bloques = []
    for it in items:
        antes = it["stock"] + sumado.get(it["product_id"], 0)
        sumado[it["product_id"]] = sumado.get(it["product_id"], 0) + it["cantidad"]
        bloques.append((it, bloque_devolucion(it, antes)))
    if len(bloques) == 1:
        it, b = bloques[0]
        return f"↩️ *Devolución*\n*{it['product_name']}*\n{b}\n\n¿Confirmas?"
    texto = f"↩️ *{len(bloques)} devoluciones*\n\n"
    for i, (it, b) in enumerate(bloques, 1):
        texto += f"{i}. *{it['product_name']}*\n   " + b.replace("\n", "\n   ") + "\n\n"
    return texto + "¿Confirmas todas?"

def avanzar_devoluciones(usuario, items, cola):
    """Resuelve, en orden, las devoluciones pedidas (`cola` = [{"producto_query", "cantidad"}]) y las suma a `items` (las ya elegidas).
    Se detiene en la primera venta ambigua: guarda `seleccion_devolucion` con lo ya resuelto y lo que falta por preguntar.
    Si todo queda resuelto, guarda el lote como `devolucion` (pendiente de confirmar). → (avisos, pregunta o None, items)."""
    avisos = []
    cola = list(cola)
    while cola:
        q = cola.pop(0)
        query, cantidad = q["producto_query"], q.get("cantidad")
        ventas = ventas_para_devolver(query)
        if not ventas:
            avisos.append(f"❌ No encuentro ventas recientes de *{query}* en el registro del bot (guarda las últimas {MAX_SALES_LOG} ventas).")
            continue
        # Una venta de este producto que ya está en el lote → se corrige su cantidad ("no, eran las dos"), no se pregunta ni se duplica
        ya = next((it for it in items if it["orden_id"] in {v["orden_id"] for v in ventas}), None)
        if ya:
            items, err = poner_en_lote(items, ya, cantidad if cantidad is not None else ya["cantidad"])
            if err:
                avisos.append(err)
            continue
        nombres = nombres_de_ordenes([v["orden_id"] for v in ventas])
        opciones = [{
            "orden_id": v["orden_id"], "orden_nombre": nombres.get(v["orden_id"], f"#{v['orden_id']}"),
            "product_id": v["product_id"], "product_name": v["product_name"],
            "restante": v["cantidad"] - v.get("devuelto", 0), "fecha": fecha_colombia(v["timestamp"]),
            "precio": None,
        } for v in ventas]
        # precio de cada orden (para mostrar el efecto en el total)
        for op, v in zip(opciones, ventas):
            lin = odoo_search_read("sale.order.line", [["order_id", "=", v["orden_id"]], ["product_id", "=", v["product_id"]]], ["price_unit"], limit=1)
            op["precio"] = lin[0]["price_unit"] if lin else None
        if len(opciones) == 1:
            items, err = poner_en_lote(items, opciones[0], cantidad)
            if err:
                avisos.append(err)
            continue
        guardar_pendiente(usuario, {"tipo": "seleccion_devolucion", "cantidad": cantidad, "opciones": opciones,
                                    "items": items, "cola": cola})
        texto = f"Encontré {len(opciones)} ventas de *{query}*. ¿De cuál es la devolución?\n\n"
        for i, op in enumerate(opciones, 1):
            texto += f"{i}. {op['orden_nombre']} — {op['fecha']} — {precio_venta_txt(op['restante'], op['precio'])}\n"
        return avisos, texto + "\nDime el número.", items
    if items:
        guardar_pendiente(usuario, {"tipo": "devolucion", "items": items})
    return avisos, None, items

def soltar_pregunta_devolucion(usuario):
    """Una pregunta '¿de cuál venta?' que quedó sin responder no se arrastra a un mensaje nuevo: se descarta y se conserva lo ya elegido."""
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") == "seleccion_devolucion":
        if pend.get("items"):
            guardar_pendiente(usuario, {"tipo": "devolucion", "items": pend["items"]})
        else:
            limpiar_pendiente(usuario)

def handler_devolver(usuario, args):
    """Una llamada por producto. Con varios productos en el mismo mensaje se arma UN lote con una sola confirmación;
    el texto con el lote lo arma quien procesa el mensaje al final (marcador `__BATCH_DEVOLUCION__`, como `__BATCH_VENTA__`)."""
    query = (args.get("producto_query") or "").strip()
    cantidad = args.get("cantidad")
    if not query:
        return "¿Qué producto devolvió el cliente? Dime el código o el nombre."
    if cantidad is not None and cantidad < 1:
        return "La cantidad devuelta debe ser al menos 1."
    pend = obtener_pendiente(usuario)
    items, cola = [], []
    if pend and pend.get("tipo") in ("devolucion", "seleccion_devolucion"):
        items = list(pend.get("items", []))
        cola = list(pend.get("cola", []))
    nueva = {"producto_query": query, "cantidad": cantidad}
    if pend and pend.get("tipo") == "seleccion_devolucion":
        # Otro producto del MISMO mensaje: espera su turno detrás de la pregunta que ya se hizo
        pend["cola"] = cola + [nueva]
        guardar_pendiente(usuario, pend)
        return "__BATCH_DEVOLUCION__"
    avisos, pregunta, items = avanzar_devoluciones(usuario, items, [nueva])
    if pregunta:
        return "\n".join(avisos + [pregunta])
    return "\n".join(avisos + ["__BATCH_DEVOLUCION__"]) if items else "\n".join(avisos)

def vista_devoluciones(usuario):
    """Texto del lote de devoluciones pendiente de confirmar (None si no hay)."""
    pend = obtener_pendiente(usuario)
    if pend and pend.get("tipo") == "devolucion" and pend.get("items"):
        return formatear_devoluciones(pend["items"])
    return None

def resultado_devolucion(it, r):
    """Texto del resultado de UNA devolución (según lo que contestó `aplicar_devolucion`)."""
    if not r["ok"]:
        return f"❌ *{it['product_name']}*\nNo hice la devolución: {r['msg']}\nNo moví el stock. Revisa la orden en Odoo."
    if r["error_stock"]:
        return (f"⚠️ *{it['product_name']}*\nLa orden ya se actualizó ({r['msg']}) pero NO pude sumar el stock: {r['error_stock']}\n"
                f"Súmale {it['cantidad']} al stock de *{it['product_name']}* a mano.")
    return (f"*{it['product_name']}*\n{r['msg']}\n📦 Stock: {r['antes']} → {r['despues']}"
            + (f"\n{r['aviso']}" if r["aviso"] else ""))

def ejecutar_devolucion(usuario, pend):
    items = pend.get("items", [])
    limpiar_pendiente(usuario)   # primero: un "sí" repetido no puede aplicar el mismo lote dos veces
    bloques, buenas = [], 0
    for it in items:
        try:
            r = aplicar_devolucion(it["orden_id"], it["product_id"], it["cantidad"])
        except Exception as e:
            log.error(f"Error en devolución de la orden {it['orden_id']}: {e}")
            log.error(traceback.format_exc())
            r = {"ok": False, "msg": str(e)}
        buenas += 1 if r["ok"] and not r["error_stock"] else 0
        bloques.append(resultado_devolucion(it, r))
    if len(items) == 1:
        return ("✅ Devolución registrada\n" if buenas else "") + bloques[0]
    if buenas == len(items):
        encabezado = f"✅ {len(items)} devoluciones registradas"
    elif buenas == 0:
        encabezado = "❌ No se registró ninguna devolución"
    else:
        encabezado = f"⚠️ Se registraron {buenas} de {len(items)} devoluciones (revisa las marcadas)"
    return encabezado + "\n\n" + "\n\n".join(bloques)

def atajo_seleccion_numero(usuario, texto):
    """«3» / «el 3» / «el segundo» como respuesta a «Encontré varios productos…» (venta o actualización): se elige esa opción sin pasar por el modelo, que a veces
    repetía la búsqueda con el nombre y devolvía la misma lista (17/10 «3», «1», «1»). Solo con una lista de elegir abierta; con una lista de búsqueda o sin lista
    el número sigue yendo al modelo."""
    n = opcion_suelta(texto)
    pend = obtener_pendiente(usuario) if n else None
    if not pend or pend.get("tipo") not in ("seleccion_venta", "seleccion_actualizacion"):
        return None
    resultado = handler_confirmar(usuario, {"opcion": n})
    SUGERENCIA_EJECUTADA[usuario] = {"nombre": "confirmar", "args": {"opcion": n}, "resultado": resultado}   # (para el historial, como en atajo_sugerencia)
    return resultado

def atajo_seleccion_devolucion(usuario, texto):
    """'la 2' / '2' / 'la segunda' como respuesta a '¿De cuál es la devolución?' (sin pasar por el modelo)."""
    pend = obtener_pendiente(usuario)
    if not pend or pend.get("tipo") != "seleccion_devolucion":
        return None
    t = (texto or "").strip().lower().strip(".!?¡¿ ")
    m = re.fullmatch(r"(?:(?:la|el|opci[oó]n|n[uú]mero)\s*)?(\d{1,2})", t)
    if m:
        return handler_confirmar(usuario, {"opcion": int(m.group(1))})
    nums = detectar_ordinales(t)
    if len(nums) == 1 and len(t.split()) <= 3:
        return handler_confirmar(usuario, {"opcion": nums[0]})
    return None

def handler_resumen(usuario, args):
    return resumen_ventas_periodo(args.get("periodo", "hoy"))

def handler_ayuda(usuario, args):
    return TEXTO_AYUDA

HANDLERS = {
    "buscar": handler_buscar,
    "vender": handler_vender,
    "confirmar": handler_confirmar,
    "crear": handler_crear,
    "actualizar": handler_actualizar,
    "registrar_sin_stock": handler_sin_stock,
    "reporte_sin_stock": handler_reporte_sin_stock,
    "deshacer_ultima_venta": handler_deshacer,
    "devolver": handler_devolver,
    "resumen_ventas": handler_resumen,
    "ayuda": handler_ayuda,
}

# ──────────────────────────────────────────
# OPENAI - PROMPT Y TOOLS
# ──────────────────────────────────────────

_CATS_LIST = "\n".join(f"- {k}" for k in CATEGORIAS.keys())
_CATS_ENUM = list(CATEGORIAS.keys())

SYSTEM_PROMPT = f"""You are Radibot, internal assistant for Radibombas — a Colombian spare parts store specializing in vehicle cooling systems (radiators, water pumps, hoses, fans, thermostats, caps).

Your job: help employees search products, register sales, manage inventory, and view reports — in natural Spanish.

STYLE: brief and direct. Use emojis only when they add value, never as sign-off.

---

BRAND CORRECTIONS (fix typos before searching)

- Chevrolet (Aveo, Spark, Corsa, Luv, Captiva, Sail, Optra, NPR, NHR, NKR, NQR)
- Renault (Logan, Sandero, Duster, Twingo, Symbol, Stepway, Megane, Koleos)
- Toyota (Hilux, Corolla, Fortuner, Land Cruiser, Prado, RAV4)
- Mazda (BT50, CX5, Mazda 3, Mazda 6, B2000, B2200, B2600)
- Ford (Ranger, Explorer, Fiesta, Focus, EcoSport, F150)
- Hyundai (Tucson, Accent, i10, Elantra, H100)
- Kia (Sportage, Sorento, Picanto, Cerato, Rio)
- Nissan (Frontier, Pathfinder, March, Sentra, X-Trail, Navara)
- Mitsubishi (Montero, L200, Outlander, Lancer)
- Volkswagen/VW (Golf, Jetta, Tiguan, Amarok, Gol)
- Suzuki (Vitara, Grand Vitara, Swift, Jimny)
- Kenworth (T800, T680, T370, W900)
- Freightliner, International, Hino

Examples: "Kemborth" → "Kenworth", "Chevroler" → "Chevrolet", "Toyata" → "Toyota"

---

WHEN TO USE EACH TOOL

**buscar** — Any question about products, prices, stock, availability.
- PHOTOS: if the user asks to SEE a photo/image ("dame la imagen de 5490", "quiero ver la foto de las mangueras superiores del aveo", "foto del 5492"), call `buscar` with the SAME query/categoria/atributos rules as any search (a code → `query` only) plus `con_imagen: true`. NEVER put "foto", "imagen", "dame" or "quiero ver" in `query`. Never say you cannot show images — the bot sends them right after your tool call.

CATEGORIES (exact names — use these in `categoria` parameter):
{_CATS_LIST}

CATEGORY MATCHING RULES:
- Match user's words to the MOST SPECIFIC category.
- "manguera de tramo" → "mangueras tramos"
- "manguera en rollo" → "mangueras rollos"
- "manguera corrugada" → "mangueras corrugadas"
- "manguera del corsa/aveo/etc" → "mangueras para vehículo"
- "manguera" alone → "mangueras" (searches all subcategories)
- "tapa de 13 lbs" → "tapas de radiador"
- "tapa de [vehículo]" → "tapas de tarro de reserva"

PARAMETER USAGE:
- Pass the category in `categoria`, descriptive words (brand, model, color) in `query`, and measurements/variants in `atributos`.

MEASUREMENT RULE: Any measurement (diameter, length, pressure) goes in `atributos`, NEVER in `query`.
- Convert decimals to fractions BEFORE putting in atributos: 1.5 → "1 1/2", 2.5 → "2 1/2", 0.5 → "1/2"
- Formats accepted: "1/4", "5/16", "3/8", "1/2", "5/8", "3/4", "7/8", "1", "1 1/4", "1 1/2", "1 3/4", "2", "2 1/4", "2 1/2", and so on

Examples by category:
- "radiador corsa" → categoria: "radiadores", query: "corsa"
- "bomba toyota hilux" → categoria: "bombas de agua", query: "toyota hilux"
- "manguera corrugada 1.5 x 26" → categoria: "mangueras corrugadas", query: "", atributos: ["1 1/2", "26"]
- "manguera corrugada 2 por 30" → categoria: "mangueras corrugadas", query: "", atributos: ["2", "30"]
- "manguera rollo negra 5/8" → categoria: "mangueras rollos", query: "negra", atributos: ["5/8"]
- "manguera tramo azul 3/4" → categoria: "mangueras tramos", query: "azul", atributos: ["3/4"]
- "codo rojo 2 1/2 por 12" → categoria: "codos", query: "rojo", atributos: ["2 1/2", "12"]   (diameter first, then length)
- "codo negro de 3" → categoria: "codos", query: "negro", atributos: ["3"]   (only ONE measure: the bot answers with the table of the other one)
- "codo azul 1 3/4" → categoria: "codos", query: "azul", atributos: ["1 3/4"]
- "tapa 13 lbs" → categoria: "tapas de radiador", query: "tapa", atributos: ["13"]
- "tapa pequeña alta 16 libras" → categoria: "tapas de radiador", query: "tapa pequeña", atributos: ["Alta", "16"]
- "tapa aveo" → categoria: "tapas de tarro de reserva", query: "aveo"
- "Semicodo 3 pulgadas" → categoria: "codos", query: "semicodo", atributos: ["3"]

- For product codes (numbers like 5533 OR codes with letters like TR-08, BA-103): pass ONLY the code as `query`. NEVER pass `categoria` with a code.
- Prefixes in codes (TR-, BA-, etc.) are NOT category indicators.
- Vehicle brands/models (Corsa, Aveo, Hilux, etc.) are NEVER attributes. Put them in `query`.
- Use the SINGULAR for position words in `query`: "superiores" → "superior", "inferiores" → "inferior" (product names are singular).

QUERY MUST KEEP EVERY DESCRIPTIVE WORD, in the same order the user said them. NEVER drop part/position words such as inferior, superior, entrada, salida, calefacción, bomba, radiador — they tell which exact product the user wants. The bot searches them in order (e.g. "inferior aveo" → name like '%inferior%aveo%').
- "manguera inferior aveo" → categoria: "mangueras para vehiculo", query: "inferior aveo"   (NOT just "aveo")
- "manguera superior del corsa" → categoria: "mangueras para vehiculo", query: "superior corsa"
- "manguera de calefacción salida aveo" → categoria: "mangueras para vehiculo", query: "calefacción salida aveo"

ATRIBUTE RULE: The `atributos` parameter is ONLY for variant products:
  - Mangueras corrugadas: diameter and length like "1 1/2", "24"
    - A measure NEVER goes in `query`/`producto_query`: "corrugada de 40" → atributos ["40"]; "de dos y media" / "de 2.5" → ["2 1/2"]; "de dos y media por 40" → ["2 1/2", "40"] (diameter first, then length).
    - If the user gives only ONE of the two, call the tool anyway with that one: the bot answers by itself with a table of the missing one (price and stock of each) and asks which. Show that answer as is. When the user then says the missing value ("la de 2 y media"), call again with BOTH values, combining it with the one from the earlier message.
  - Mangueras rollos: diameter like "3/8", "1/2", "3/4", "5/8"
  - Mangueras tramos: diameter like "1", "2", "1 1/2", "3 3/4"
  - Tapas: "Alta"/"Baja" and pressure like "13", "16", "20"
  - Codo negro and codo rojo: diameter AND length, like "2 1/2" and "20" (lengths: negro 12, 20, 28; rojo 12, 16) → atributos ["2 1/2", "20"] (diameter first, then length); the colour goes in `query` ("negro", "rojo"). A measure NEVER goes in `query`: "codo negro de dos y media por veinte" → query "negro", atributos ["2 1/2", "20"]. If the user gives only ONE of the two, call the tool anyway with that one: the bot answers with a table of the missing one (price and stock of each) and asks which; show that answer as is, and when the user then says the missing value ("la de 20"), call again with BOTH values and the same colour.
  - Codo azul: diameter only, like "1 1/2", "1 3/4", "2 1/2"
  - Semicodos, codos reductores and Ele: diameter like "2 1/2", "3", "1 3/4", "4" (NOT the codo negro/rojo rule)

**vender** — User says product was sold ("se vendió", "vendí", "salió").
- Pass `producto_query`, `cantidad`, AND identify category/attributes same as `buscar`.
- Examples:
  - "se vendió un radiador de aveo" → producto_query: "aveo", cantidad: 1, categoria: "radiador"
  - "se vendió 2 tapas pequeñas alta 16" → producto_query: "tapa pequeña", cantidad: 2, atributos: ["Alta", "16"]
  - "se vendió la manguera inferior del aveo" → producto_query: "inferior aveo", cantidad: 1, categoria: "mangueras para vehiculo" (keep "inferior", same QUERY rule as `buscar`)
  - "se vendió la 5533" → producto_query: "5533", cantidad: 1 (no category, just code)
- Several products in one message ("se vendió la 5490, 5492 y 5491", "2 del 5490 y 1 del 5492") → ONE `vender` call per product, each with its own exact code and quantity (the bot already handles plain code lists by itself).
- CORRECTING a sale that is still waiting for confirmation ("perdón, eran dos", "fueron 2", "mejor 3", "no, una sola"): call `vender` again for the SAME product with the TOTAL quantity the user means now. The bot REPLACES the pending quantity of that product (it does not add a second sale). Do NOT call `confirmar(cancelar=true)` first.
- NOTE: "agrega", "añade", "suma" are NOT sales — they mean adding to stock. Use `actualizar` for these.

**confirmar** — User says "sí", "confirmo", "correcto", OR picks a number ("la 1", "el 2").
- If they gave a number, pass `opcion` with that number.
- NEVER search again to confirm. The bot remembers everything.

**actualizar** — Modify existing product properties.
- Use the same `categoria` and `atributos` logic as `buscar` to find the right product.
- "llegaron 5" → `agregar_stock: 5`
- "el stock es 10" / "stock real 5" → `establecer_stock: 10`
- "el precio es X" / "cambia precio" → `nuevo_precio: X` (the SALE price the customer pays)
- "el costo es X" / "actualiza el costo a X" / "costó X" / "lo que pagamos" / "precio de compra" → `nuevo_costo: X` (what the store paid). NEVER put a cost in `nuevo_precio`: changing the sale price by mistake affects every sale.
- "esa misma" / "ese mismo" / "el mismo" = the product shown in your PREVIOUS message: `producto_query` = the exact code in its brackets (e.g. "✅ *[1237] Manguera…*" → "1237"), never its name (one name can have 13 variants).
- "el código es X" → `nuevo_codigo: X` (each VARIANT has its own code; a product with variants has no code on the template)
- "cambia el nombre" → `nuevo_nombre: X`
- Combine multiple changes of the SAME product in ONE call ("precio 40000 y stock 1" → `nuevo_precio=40000, establecer_stock=1` in a single `actualizar`, never two calls).
- CORRECTING a value while an update is still waiting for confirmation ("el stock a 1", "mejor 1", "no, el precio es 45000"): call `actualizar` again for that product with ONLY the corrected field; the bot replaces that field and keeps the others.
- NEVER use `actualizar` to cancel or remove a pending change ("cancela lo del stock", "quita el precio"): the bot handles that by itself; to cancel everything use `confirmar(cancelar=true)`.
- "agrega una", "agrega 2", "añade una" (without explicit product context) → `agregar_stock` with the quantity

**crear** — Brand new product. Ask ONLY for missing REQUIRED info.
- Required: `nombre`, `precio` (sale price), `cantidad` (initial stock), `categoria`. Optional: `costo` (purchase cost) and `referencia` (code).
- NEVER ask for the optional fields. If the user gave nombre, precio, cantidad and categoria, call `crear` right away, leaving `costo` and `referencia` out if they were not mentioned (the bot shows a preview and asks for confirmation anyway).
- If the user asks what is needed to create a product, list all six and mark `costo` and `referencia` as optional.
- Example: "crea radiador chevrolet sail, precio 350000, costo 220000, stock 1, categoría radiadores" → crear(nombre="Radiador Chevrolet Sail", precio=350000, costo=220000, cantidad=1, categoria="radiadores"). The price is what the customer pays; the cost is what the store paid.

**registrar_sin_stock** — Adds an item to the SUPPLIER ORDER LIST. ONLY use when the user EXPLICITLY says a customer came looking for something that wasn't available ("no había", "vinieron por", "cliente pidió X pero no teníamos", "no hubo", "no tenemos"), or asks to note it ("anótalo", "apunta que pidieron X"). NEVER use it because a search found nothing: the bot itself asks "¿Lo anoto?" after such a search and handles the user's "sí"/"no" — do not call this tool for that answer.

**reporte_sin_stock** — Show the supplier order list (what customers asked for and we did not have). Use when the user asks "qué me falta pedir", "lista de pedidos", "qué faltó", "demanda perdida", "qué nos pidieron y no había". Marking items as ordered ("ya pedí el 1") or removing them ("quita el 2") is handled by the bot itself right after it shows the list.

**deshacer_ultima_venta** — User says "deshacer", "me equivoqué", "cancelar última venta" (a sale registered by MISTAKE).

**devolver** — A customer RETURNED something already sold, any past sale ("el cliente devolvió la 5490", "me devolvieron 1 de la 4504", "devolución de la 5490, no era el diámetro", "no le sirvió la manguera del aveo", "devolvió una de las dos").
- Pass `producto_query` (a code goes alone, exactly as said) and `cantidad` ONLY if the user says how many units were returned; otherwise omit it.
- The bot finds the matching sale(s), shows what will change, and asks for confirmation. If there are several sales of that product it asks which one; the user answers with a number.
- Several products returned in one message ("devolvieron la 5490 y la 5492", "devolvió una 5490 y dos 4504") → ONE `devolver` call per product, each with its own `cantidad`. The bot builds ONE combined list and asks for confirmation once.
- Correcting a return that is still waiting for confirmation ("no, eran las dos") → call `devolver` again for that product with the new `cantidad`; the bot replaces it.
- NEVER use `deshacer_ultima_venta` for a customer return, and never use `devolver` for a mistaken sale ("me equivoqué").

**resumen_ventas** — User asks about sales totals/today/week/month.

**ayuda** — User asks what you can do.

---

PENDING ACTION CORRECTIONS
- If the user corrects a detail of a pending action BEFORE confirming ("perdón, 7 libras", "no, eran 2", "cámbiale a..."), simply call the same tool again with the corrected info. The previous pending action will be replaced automatically.
- NEVER call multiple tools to "undo" a pending action — pending actions are not yet applied to the system.
- Only use `deshacer_ultima_venta` AFTER a sale has been confirmed.

KEY RULES

RULE 1 — CONFIRMATION/CANCELLATION: When user responds to "¿Confirmas?":
- "sí", "si", "confirmo", "correcto", "dale", "ok" → call `confirmar()` with NO parameters
- "no", "ya no", "cancela", "olvídalo", "no quiero", "espera" → call `confirmar(cancelar=true)`. This ONLY cancels the pending action, it does NOT undo any previous confirmed sale.
- NEVER call `deshacer_ultima_venta` when there is a pending "¿Confirmas?" — that tool is ONLY for undoing already completed sales.

RULE 2 — NUMBERED SELECTION: Option numbers are ONLY 1 to 5. A number with 3 or more digits (5490, 13062, 4501) is a PRODUCT CODE, never an option number — even if a list was shown before: "se vendió la 5490" → vender(producto_query="5490", cantidad=1), NOT opcion_lista.
When user says "el primero", "la 1", "opción 2", "el segundo", "ese mismo" referring to a product from a previous list, ALWAYS use `opcion_lista` with that number. Examples:
- "el primero, actualiza stock a 3" → actualizar(opcion_lista=1, establecer_stock=3) — NO producto_query needed
- "vende la segunda" → vender(opcion_lista=2, cantidad=1) — NO producto_query needed
- "ese mismo, llegaron 5" → actualizar(opcion_lista=1, agregar_stock=5)
- "el primero" after seeing a list → confirmar(opcion=1)

RULE 3 — AFTER SEARCH: If search showed exactly 1 product and user says "se vendió", "vendí", "ese mismo", use that product's exact name as `producto_query`. If multiple were shown, ask "¿Cuál?" instead of guessing.

RULE 4 — CODES: Numeric codes (5533) or alphanumeric (TR-08, BA-103) go ONLY in `query`. Never add `categoria` with a code. When the user says a code, pass EXACTLY that code — never replace it with a similar code or a product name from an earlier list.

RULE 5 — NO INVENTION: Never invent prices, stock, or IDs. Always use tools."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "buscar",
            "description": "Search products in inventory. Use for ANY question about products, stock, prices, or availability.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search terms: ALL descriptive words the user said, in the same order (position words like inferior/superior/entrada/salida + vehicle brand/model). Never drop them. If a category word is given, OMIT it from query."},
                    "categoria": {
                        "type": "string",
                        "enum": _CATS_ENUM,
                        "description": "Product category (exact Odoo category name)",
                    },
                    "atributos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional variant attributes ONLY for: corrugadas (diameter, length), tapas (Alta/Baja and pressure), codos rojos/negros (diameter, length)",
                    },
                    "con_imagen": {
                        "type": "boolean",
                        "description": "true ONLY when the user asks to SEE the photo/image ('dame la imagen de 5490', 'quiero ver la foto de ...'). Same query/categoria rules; never put 'foto'/'imagen' in query.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vender",
            "description": "Register a sale. Use when user says a product was sold.",
            "parameters": {
                "type": "object",
                "properties": {
                    "producto_query": {"type": "string", "description": "Product search terms (without category word)"},
                    "cantidad": {"type": "integer"},
                    "categoria": {
                        "type": "string",
                        "enum": _CATS_ENUM,
                        "description": "Product category (exact Odoo category name)",
                    },
                    "atributos": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Variant attributes for corrugadas, tapas, codos rojos/negros only",
                    },
                    "opcion_lista": {
                        "type": "integer",
                        "description": "PRIORITY PARAMETER: When user says 'el primero', 'la 1', 'opción 2', 'ese mismo', etc. referring to a numbered item from a previous list, ALWAYS use this instead of searching by name. Pass 1 for first, 2 for second, etc. producto_query is NOT needed when this is provided.",
                    },
                },
                "required": ["cantidad"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmar",
            "description": "Confirm OR cancel the pending action. Use when user responds to '¿Confirmas?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "opcion": {"type": "integer", "description": "Number selected from a list (1, 2, 3...). Omit if just confirming."},
                    "cancelar": {"type": "boolean", "description": "Set to true when user says 'no', 'ya no', 'cancela', 'olvídalo', 'no quiero'. This cancels the pending action WITHOUT undoing any previous sale."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crear",
            "description": "Create a new product.",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string"},
                    "precio": {"type": "number", "description": "Sale price in COP"},
                    "costo": {"type": "number", "description": "OPTIONAL. Purchase cost in COP ('costo', 'me costó', 'lo compré a'). Never confuse it with `precio`."},
                    "cantidad": {"type": "integer", "description": "Initial stock"},
                    "categoria": {
                        "type": "string",
                        "enum": _CATS_ENUM,
                        "description": "Product category — must match exactly one of the enum values"
                    },
                    "referencia": {"type": "string", "description": "OPTIONAL. Product code"},
                },
                "required": ["nombre", "precio", "cantidad", "categoria"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "actualizar",
            "description": "Update existing product: name, sale price, purchase cost, code, or stock (add or set).",
            "parameters": {
                "type": "object",
                "properties": {
                    "producto_query": {"type": "string"},
                    "categoria": {
                        "type": "string",
                        "enum": _CATS_ENUM,
                        "description": "Product category (exact Odoo category name)",
                    },
                    "atributos": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "opcion_lista": {
                        "type": "integer",
                        "description": "PRIORITY PARAMETER: When user says 'el primero', 'la 1', 'opción 2', 'ese mismo', etc. referring to a numbered item from a previous list, ALWAYS use this instead of searching by name. Pass 1 for first, 2 for second, etc. producto_query is NOT needed when this is provided.",
                    },
                    "nuevo_nombre": {"type": "string"},
                    "nuevo_precio": {"type": "number", "description": "New SALE price in COP (what the customer pays). ONLY when the user says 'precio'. NEVER for a cost."},
                    "nuevo_costo": {"type": "number", "description": "New purchase COST in COP (what the store paid; Odoo 'Cost'). Use it when the user says 'costo', 'lo que costó', 'lo que pagamos', 'precio de compra'. It is NOT the sale price."},
                    "nuevo_codigo": {"type": "string"},
                    "agregar_stock": {"type": "integer", "description": "Add to current stock (for 'llegaron X')"},
                    "establecer_stock": {"type": "integer", "description": "Set exact final stock"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "registrar_sin_stock",
            "description": "Add to the supplier order list a product that a customer requested but we did not have. Use ONLY when the user explicitly says 'no había', 'no tenemos', 'vinieron por', 'cliente pidió pero no había' or asks to note it. NOT after a search that found nothing (the bot offers that itself).",
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre": {"type": "string", "description": "Product description as specific as possible"},
                    "unidades": {"type": "integer", "description": "Quantity the customer wanted", "default": 1},
                    "categoria": {
                        "type": "string",
                        "enum": _CATS_ENUM,
                        "description": "Product category if known"
                    },
                    "codigo": {"type": "string", "description": "Product code if customer mentioned one"}
                },
                "required": ["nombre"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reporte_sin_stock",
            "description": "Show the supplier order list (products customers requested that we did not have). Use when user asks 'qué me falta pedir', 'lista de pedidos', 'qué faltó', 'demanda perdida'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "periodo": {
                        "type": "string",
                        "enum": ["hoy", "semana", "mes", "año"],
                        "description": "Time period for the report"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "deshacer_ultima_venta",
            "description": "Undo the user's last sale because it was a MISTAKE ('deshacer', 'me equivoqué'). NOT for a customer who returns a product (use `devolver`).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "devolver",
            "description": "A customer RETURNED something that was already sold (any past sale, not only the last): the units go back to stock and that sale is cancelled, or reduced if only part is returned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "producto_query": {"type": "string", "description": "Product code or name as the user said it (a code goes alone, exactly as said)"},
                    "cantidad": {"type": "integer", "description": "Units returned. Include ONLY if the user says how many; otherwise omit (the bot assumes 1)."},
                },
                "required": ["producto_query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resumen_ventas",
            "description": "Sales summary for a period.",
            "parameters": {
                "type": "object",
                "properties": {
                    "periodo": {"type": "string", "enum": ["hoy", "semana", "mes"]},
                },
                "required": ["periodo"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ayuda",
            "description": "Show the user a guide of commands.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# ──────────────────────────────────────────
# PROCESAMIENTO CON OPENAI
# ──────────────────────────────────────────

def trim_history(historial, max_len=MAX_HISTORY):
    if len(historial) <= max_len:
        return historial
    historial = historial[-max_len:]
    while historial:
        first = historial[0]
        role = first.get("role") if isinstance(first, dict) else getattr(first, "role", None)
        tool_calls = first.get("tool_calls") if isinstance(first, dict) else getattr(first, "tool_calls", None)
        if role == "tool" or (role == "assistant" and tool_calls):
            historial = historial[1:]
        else:
            break
    return historial

def llamar_openai(historial):
    return client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + historial,
        tools=TOOLS,
        tool_choice="auto",
    )

_CONFIRMA = re.compile(r"(?i)^\s*[¡¿]?\s*(?:s[ií]+p?|dale|ok|okay|vale|confirmo|confirmado|correcto|de una|claro)(?:\s*,?\s*(?:confirmo|por favor|gracias))?\s*[.!]*\s*$")
_CANCELA = re.compile(r"(?i)^\s*(?:no|nop|cancela(?:r|lo|la)?|ya no|olv[ií]dalo|d[eé]jalo|no quiero|espera)\s*[.!]*\s*$")

def atajo_confirmacion(usuario, texto):
    """'Sí' / 'No' (solos) ante un '¿Confirmas?' pendiente: se ejecuta sin pasar por el modelo.
    Antes el modelo a veces volvía a llamar `vender` en vez de `confirmar` y duplicaba la venta pendiente."""
    pend = obtener_pendiente(usuario)
    if not pend or pend.get("tipo") not in ("venta", "actualizacion", "actualizacion_batch", "creacion", "devolucion"):
        return None
    if _CONFIRMA.match(texto or ""):
        return handler_confirmar(usuario, {})
    if _CANCELA.match(texto or ""):
        return handler_confirmar(usuario, {"cancelar": True})
    return None

_AFIRMA = re.compile(r"(?i)\b(?:s[ií]+p?|dale|ok|okay|confirm\w*|correcto|de una|claro|listo|h[aá]gale|adelante|hazlo|apl[ií]ca\w*|procede|exacto|perfecto|as[ií] es|eso es|est[aá] bien|todo bien|afirmativo|aprobado|ejecuta\w*|registra\w*|hecho|guarda\w*)\b")
_TIPOS_EJECUTABLES = ("venta", "actualizacion", "actualizacion_batch", "creacion", "devolucion")

def bloqueo_confirmacion_dudosa(usuario, texto, tool_calls):
    """Guarda contra un `confirmar` que el usuario NO dijo. El modelo a veces llama `confirmar` por su cuenta: junto con `actualizar`
    en el mismo turno (con 'El stock a 1' aplicó los cambios sin que nadie dijera sí: 2 de 12 pruebas) o con un '1' que tomó por
    número de opción. Un `confirmar` que EJECUTA algo solo pasa si (a) en este mismo mensaje no se armó ni cambió ningún pendiente y
    (b) el mensaje del usuario suena a afirmación. → None = dejar pasar; si no, el texto a mostrar ('' = nada)."""
    pend = obtener_pendiente(usuario)
    if not pend or pend.get("tipo") not in _TIPOS_EJECUTABLES:
        return None
    if any(tc.function.name in ("vender", "actualizar", "crear", "devolver") for tc in tool_calls):
        return ""   # el usuario aún no vio cómo quedó: se le muestra la vista previa y confirma después
    if not _AFIRMA.search(texto or ""):
        return "Tienes algo pendiente por confirmar. Responde *sí* para aplicarlo o *no* para cancelarlo."
    return None

def procesar_con_openai(usuario, texto, historial):
    texto_original = texto
    try:
        texto = unir_codigo_partido(texto)
        if texto != texto_original:
            log.info(f"CÓDIGO en pedazos unido: «{texto_original}» → «{texto}»")
    except Exception as e:
        log.warning(f"unir_codigo_partido falló: {e}")
        texto = texto_original
    codigo_previo = codigo_unico_en_ultima_respuesta(historial)   # para "esa misma" / "ese mismo"
    # Atajos deterministas (no pasan por el modelo): "sí"/"no" ante un ¿Confirmas?, "foto de la 2", "se vendió la 5490, 5492, 5491"
    for atajo in (atajo_sugerencia, atajo_faltante, atajo_lista_pedidos, atajo_codigo_suelto, atajo_confirmacion, atajo_quitar_cambio, atajo_seleccion_devolucion, atajo_seleccion_numero, atajo_foto_opcion, atajo_venta_multiple, atajo_corrugada_sin_medida):
        try:
            resp_atajo = atajo(usuario, texto)
        except Exception as e:
            log.error(f"Error en {atajo.__name__}: {e}")
            log.error(traceback.format_exc())
            resp_atajo = None
        if resp_atajo:
            log.info(f"ATAJO {atajo.__name__}")
            historial.append({"role": "user", "content": texto})
            if atajo is atajo_venta_multiple:
                # Dejar el historial igual que si el modelo hubiera llamado `vender` por cada código: así, al ver
                # "¿Confirmas todas?" y un "sí", sabe que la venta ya está pendiente y no la repite.
                llamadas = [
                    {"id": f"atajo_{i}", "type": "function",
                     "function": {"name": "vender", "arguments": json.dumps({"producto_query": c, "cantidad": q}, ensure_ascii=False)}}
                    for i, (c, q) in enumerate(partir_codigos_venta(texto) or [])
                ]
                if llamadas:
                    historial.append({"role": "assistant", "content": None, "tool_calls": llamadas})
                    for ll in llamadas:
                        historial.append({"role": "tool", "tool_call_id": ll["id"], "content": "__BATCH_VENTA__"})
            if atajo in (atajo_sugerencia, atajo_seleccion_numero):
                ll = SUGERENCIA_EJECUTADA.pop(usuario, None)
                if ll:   # el «sí» ejecutó la consulta corregida: el historial la muestra como una llamada normal
                    historial.append({"role": "assistant", "content": None, "tool_calls": [
                        {"id": "atajo_sug", "type": "function", "function": {"name": ll["nombre"], "arguments": json.dumps(ll["args"], ensure_ascii=False)}}]})
                    historial.append({"role": "tool", "tool_call_id": "atajo_sug", "content": ll["resultado"]})
            historial.append({"role": "assistant", "content": resp_atajo})
            return resp_atajo, trim_history(historial)

    historial.append({"role": "user", "content": texto})

    try:
        response = llamar_openai(historial)
    except Exception as e:
        log.error(f"Error OpenAI, reseteando historial: {e}")
        historial = [{"role": "user", "content": texto}]
        response = llamar_openai(historial)

    mensaje = response.choices[0].message

    if mensaje.tool_calls:
        # Serializar mensaje del assistant
        mensaje_dict = {
            "role": "assistant",
            "content": mensaje.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in mensaje.tool_calls
            ],
        }
        historial.append(mensaje_dict)

        if any(tc.function.name == "devolver" for tc in mensaje.tool_calls):
            soltar_pregunta_devolucion(usuario)   # una pregunta "¿de cuál venta?" sin responder no se arrastra a este mensaje

        resultados = []
        for tool_call in mensaje.tool_calls:
            nombre = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments)
            except Exception:
                args = {}
            log.info(f"TOOL: {nombre}({args})")

            conv = buscar_que_era_venta(nombre, args, texto)
            if conv:
                log.info(f"'se vendió…' llegó como buscar → se trata como vender: {conv[1]}")
                nombre, args = conv
            # "la 5490" es un código: el modelo no puede tomarlo por opción de lista ni cambiarlo por otro producto
            n_llamadas = sum(1 for tc in mensaje.tool_calls if tc.function.name == nombre)
            if corregir_por_codigo(nombre, args, texto, n_llamadas):
                log.info(f"CÓDIGO corregido por la guarda → {nombre}({args})")
            if fijar_codigos_escritos(nombre, args, texto):
                log.info(f"CÓDIGO escrito restituido (el modelo lo había cambiado) → {nombre}({args})")

            if completar_palabras_codo(nombre, args, texto):
                log.info(f"COLOR / «semicodo» devuelto a la búsqueda → {nombre}({args})")
            if completar_medidas_desde_texto(nombre, args, texto):
                log.info(f"MEDIDAS leídas del mensaje → {nombre}({args})")
            if completar_medida(usuario, nombre, args, texto):
                log.info(f"MEDIDA completada con lo que se preguntó → {nombre}({args})")
            if corregir_medida_suelta(nombre, args):
                log.info(f"MEDIDA suelta movida a atributos → {nombre}({args})")
            if corregir_ese_mismo(nombre, args, texto, codigo_previo):
                log.info(f"'ese mismo' → producto {codigo_previo} (código del mensaje anterior): {nombre}({args})")
            if costo_mandado_como_precio(nombre, args, texto):
                log.info(f"COSTO enviado como precio por el modelo → corregido a nuevo_costo: {args}")

            # "dame la foto de…": se envían imágenes (decidido por el texto del usuario, no por el modelo)
            if nombre == "buscar" and pide_imagen(texto):
                args["con_imagen"] = True
                args["query"] = quitar_palabras_imagen(args.get("query", ""))
                args["_texto"] = texto   # para filtrar_por_distintivos (USA / CHINA…)
                log.info(f"PIDE FOTO → buscar({args})")

            # Auto-detectar referencia ordinal si la IA no la pasó
            if nombre in ("vender", "actualizar") and not args.get("opcion_lista"):
                ordinal = detectar_ordinal(texto)
                if not ordinal and opcion_suelta(texto):   # un dígito solo («3») tras una lista de varias opciones
                    pend_lista = obtener_pendiente(usuario) or {}
                    if pend_lista.get("tipo") in ("lista_productos", "seleccion_venta", "seleccion_actualizacion") and opcion_suelta(texto) <= len(pend_lista.get("opciones") or []) and len(pend_lista.get("opciones") or []) >= 2:
                        ordinal = opcion_suelta(texto)
                if ordinal:
                    pendiente_actual = obtener_pendiente(usuario)
                    if pendiente_actual and pendiente_actual.get("tipo") in ("lista_productos", "seleccion_venta", "seleccion_actualizacion"):
                        args["opcion_lista"] = ordinal
                        log.info(f"AUTO opcion_lista={ordinal} detectado en: '{texto}'")

            if nombre == "confirmar" and not args.get("cancelar"):
                bloqueo = bloqueo_confirmacion_dudosa(usuario, texto, mensaje.tool_calls)
                if bloqueo is not None:
                    log.warning(f"CONFIRMAR bloqueado (el usuario no confirmó): '{texto}' args={args}")
                    historial.append({"role": "tool", "tool_call_id": tool_call.id,
                                      "content": bloqueo or "No se aplicó: falta la confirmación del usuario."})
                    resultados.append(bloqueo)
                    continue

            handler = HANDLERS.get(nombre)
            if handler:
                try:
                    resultado = handler(usuario, args)
                except Exception as e:
                    log.error(f"Error ejecutando {nombre}: {e}")
                    log.error(traceback.format_exc())
                    resultado = f"❌ Error: {e}"
            else:
                resultado = "Función no reconocida."

            historial.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": resultado,
            })
            resultados.append(resultado)

        # Los marcadores de lote (ventas o devoluciones) se reemplazan por UN solo resumen con todo lo pendiente
        partes, hay_venta, hay_devolucion, hay_actualizacion = [], False, False, False
        for r in resultados:
            if r == "__BATCH_VENTA__":
                hay_venta = True
                continue
            if r == "__BATCH_ACTUALIZACION__":
                hay_actualizacion = True
                continue
            if "__BATCH_DEVOLUCION__" in r:
                hay_devolucion = True
                r = r.replace("__BATCH_DEVOLUCION__", "").strip()
            if r and r not in partes:   # (el modelo a veces repite la llamada y el mismo aviso saldría varias veces)
                partes.append(r)
        if hay_venta:
            vista = vista_previa_venta(usuario)
            if vista:
                partes.append(vista)
        if hay_devolucion:
            vista = vista_devoluciones(usuario)
            if vista:
                partes.append(vista)
        if hay_actualizacion:
            vista = vista_actualizaciones(usuario)
            if vista:
                partes.append(vista)
        texto_respuesta = "\n\n".join(partes)
    else:
        texto_respuesta = mensaje.content

    historial.append({"role": "assistant", "content": texto_respuesta})
    historial = trim_history(historial)
    return texto_respuesta, historial

# ──────────────────────────────────────────
# WHATSAPP
# ──────────────────────────────────────────

def enviar_whatsapp(numero, mensaje):
    url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": mensaje},
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        return r
    except Exception as e:
        log.error(f"Error enviando WhatsApp: {e}")
        return None

def preparar_imagen_para_whatsapp(img_bytes):
    """WhatsApp solo muestra imágenes JPEG o PNG (8 bits). Odoo guarda algunas fotos en WebP (43 de 373 el 2026-10-07: las subidas
    desde su pantalla): la API las aceptaba (200) pero el cliente no las mostraba. Las JPEG y las PNG normales se envían tal cual,
    como siempre; cualquier otro formato (WebP, GIF, PNG raro) se convierte a JPEG, con fondo blanco si tenía transparencia.
    → (bytes, tipo MIME) o None si el archivo no es una imagen utilizable (ilegible o diminuta, de menos de 50 px)."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(img_bytes))
        formato, modo, (ancho, alto) = im.format, im.mode, im.size
        if max(ancho, alto) < 50:
            return None
        if formato == "JPEG" or (formato == "PNG" and modo in ("RGB", "RGBA")):
            return img_bytes, "image/jpeg"   # (las PNG siempre se enviaron así y WhatsApp las muestra bien)
        im.load()
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            fondo = Image.new("RGB", im.size, (255, 255, 255))
            fondo.paste(im, mask=im.getchannel("A"))
            im = fondo
        else:
            im = im.convert("RGB")
        im.thumbnail((1920, 1920))
        salida = io.BytesIO()
        im.save(salida, "JPEG", quality=92, optimize=True)
        log.info(f"Imagen {formato} {ancho}x{alto} convertida a JPEG ({len(img_bytes)} → {salida.tell()} bytes)")
        return salida.getvalue(), "image/jpeg"
    except Exception as e:
        log.error(f"No pude interpretar la imagen ({len(img_bytes)} bytes): {e}")
        return None

def enviar_imagen_whatsapp(numero, tmpl_id, caption=""):
    log.info(f"ENVIANDO IMAGEN template_id={tmpl_id} a {numero}")
    try:
        # Descargar imagen vía XML-RPC (usa la misma conexión que el bot)
        uid, models = odoo_connect()
        result = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.template', 'read',
            [[tmpl_id]], {'fields': ['image_1920']}
        )
        img_b64 = result[0].get('image_1920') if result else None
        if not img_b64:
            log.info(f"Sin imagen para template {tmpl_id}")
            return None

        img_bytes = base64.b64decode(img_b64)
        log.info(f"Imagen descargada: {len(img_bytes)} bytes")
        preparada = preparar_imagen_para_whatsapp(img_bytes)
        if not preparada:
            log.info(f"Imagen de template {tmpl_id} no utilizable (ilegible o diminuta)")
            return None
        img_bytes, mime = preparada

        # Subir a WhatsApp
        upload_url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/media"
        headers = {"Authorization": f"Bearer {WA_TOKEN}"}
        upload_resp = requests.post(upload_url, headers=headers, files={
            "file": ("producto.jpg", io.BytesIO(img_bytes), mime),
            "messaging_product": (None, "whatsapp"),
            "type": (None, mime),
        }, timeout=15)

        if upload_resp.status_code != 200:
            log.error(f"Error subiendo imagen: {upload_resp.text}")
            return None

        media_id = upload_resp.json().get("id")
        if not media_id:
            log.error("No se obtuvo media_id")
            return None

        # Enviar mensaje con imagen
        msg_url = f"https://graph.facebook.com/v19.0/{WA_PHONE_ID}/messages"
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "image",
            "image": {"id": media_id, "caption": caption},
        }
        r = requests.post(
            msg_url,
            headers={**headers, "Content-Type": "application/json"},
            json=data, timeout=10
        )
        log.info(f"Imagen enviada: {r.status_code}")
        return r

    except Exception as e:
        log.error(f"Error enviando imagen: {e}")
        log.error(traceback.format_exc())
        return None

mensajes_procesados = set()

@app.get("/webhook")
async def verificar_webhook(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == WA_VERIFY_TOKEN:
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("Token invalido", status_code=403)

def firma_valida(cuerpo, cabecera):
    """Meta firma cada webhook: X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(cuerpo, App Secret)."""
    if not cabecera or not cabecera.startswith("sha256="):
        return False
    esperada = hmac.new(WA_APP_SECRET.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, cabecera[len("sha256="):])

@app.post("/webhook")
async def recibir_mensaje(request: Request):
    cuerpo = await request.body()
    if WA_APP_SECRET and not firma_valida(cuerpo, request.headers.get("X-Hub-Signature-256")):
        log.warning("Webhook con firma de Meta inválida o ausente: rechazado")
        return PlainTextResponse("Firma invalida", status_code=403)
    data = json.loads(cuerpo)
    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            for st in value.get("statuses", []):
                if st.get("status") == "failed":
                    log.warning(f"WhatsApp NO entregó el mensaje {st.get('id')} a {st.get('recipient_id')}: {json.dumps(st.get('errors'), ensure_ascii=False)[:400]}")
            return {"status": "ok"}

        mensaje = value["messages"][0]

        # Solo responde a los números autorizados; al resto lo ignora en silencio (sin respuesta, sin gastar OpenAI)
        if mensaje.get("from", "") not in ALLOWED_NUMBERS:
            log.warning(f"Número NO autorizado ignorado: {mensaje.get('from')} (tipo={mensaje.get('type')})")
            return {"status": "ok"}

        mensaje_id = mensaje["id"]

        # Deduplicación con tamaño controlado
        if mensaje_id in mensajes_procesados:
            return {"status": "ok"}
        mensajes_procesados.add(mensaje_id)
        if len(mensajes_procesados) > 1000:
            # Limpia los más viejos manteniendo solo los últimos 500
            mensajes_procesados.clear()

        tipo = mensaje.get("type")
        numero = mensaje["from"]

        if tipo == "text":
            texto = mensaje["text"]["body"]
        elif tipo == "audio":
            audio_id = mensaje["audio"]["id"]
            try:
                texto = transcribir_audio(audio_id)
            except openai.OpenAIError as e:
                log.error(f"OpenAI no pudo transcribir el audio: {e}")
                enviar_whatsapp(numero, mensaje_error_openai(e))
                return {"status": "ok"}
            if not texto:
                enviar_whatsapp(numero, "No pude entender el audio. ¿Puedes escribirlo?")
                return {"status": "ok"}
            log.info(f"TRANSCRIPCIÓN: {texto}")
        else:
            return {"status": "ok"}

        log.info(f"[{numero}] → {texto}")

        conversaciones = cargar_conversaciones()
        historial = conversaciones.get(numero, [])
        try:
            respuesta, historial_actualizado = procesar_con_openai(numero, texto, historial)
        except openai.OpenAIError as e:
            log.error(f"OpenAI no respondió: {e}")
            enviar_whatsapp(numero, mensaje_error_openai(e))
            return {"status": "ok"}
        conversaciones[numero] = historial_actualizado
        guardar_conversaciones(conversaciones)

        # Fotos solo cuando el usuario las pide: una imagen por producto con su descripción, luego los avisos
        plan = IMAGENES_A_ENVIAR.pop(numero, None)
        if plan:
            fallidas = []
            for item in plan["items"]:
                r = enviar_imagen_whatsapp(numero, item["tmpl_id"], item["caption"])
                if r is None or r.status_code != 200:
                    fallidas.append(item["caption"].split("\n")[0])
            texto_final = plan["texto"]
            if fallidas:
                texto_final += ("\n\n" if texto_final else "") + "⚠️ No pude enviar la foto de:\n" + "\n".join(f"• {f}" for f in fallidas)
            if texto_final:
                enviar_whatsapp(numero, texto_final)
            log.info(f"[{numero}] ← 📷 {len(plan['items']) - len(fallidas)} foto(s) enviada(s), {len(fallidas)} fallida(s)")
        else:
            enviar_whatsapp(numero, respuesta)
            log.info(f"[{numero}] ← {respuesta[:120]}")

    except Exception as e:
        log.error(f"Error procesando webhook: {e}")
        log.error(traceback.format_exc())

    return {"status": "ok"}
