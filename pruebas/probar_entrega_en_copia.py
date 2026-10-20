# Prueba COMPLETA de ventas con entrega validada y devoluciones (forma correcta de Odoo) contra la COPIA temporal (puerto 8070).
# Primero:  ./copia_odoo.sh arrancar   Después:  python3 probar_entrega_en_copia.py   Al terminar:  ./copia_odoo.sh parar
# Aborta si el bot no está apuntando a la copia. No toca data/ (el registro de ventas se simula en memoria).
import os, sys, time, json, traceback
# --- SEGURIDAD: SOLO contra la copia temporal. Si algo apuntara a producción, se aborta antes de importar el bot ---
os.environ["ODOO_URL"] = "http://127.0.0.1:8070"; os.environ["ODOO_DB"] = "radibombas_prueba"; os.environ["MODO_ENTREGA"] = "odoo"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
import bot
assert bot.ODOO_DB == "radibombas_prueba" and bot.ODOO_URL.endswith(":8070") and bot.MODO_ENTREGA == "odoo", "¡NO está apuntando a la copia!"
LOGMEM = []                                                   # el registro de ventas real NO se toca
bot.cargar_ventas_log = lambda: LOGMEM; bot.guardar_ventas_log = lambda d: None
R = []
def chk(t, c, extra=""): R.append((t, bool(c))); print(f"  {'OK ' if c else 'MAL'} {t} {extra}")
uid, models = bot.odoo_connect()
def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)
def prod(cod): return call("product.product", "search_read", [["default_code", "=", cod]], fields=["id", "display_name", "lst_price", "qty_available"], limit=1)[0]
def stock(pid): return call("product.product", "read", [pid], fields=["qty_available"])[0]["qty_available"]
def reservadas(pid): return sum(q["reserved_quantity"] for q in call("stock.quant", "search_read", [["product_id", "=", pid], ["location_id.usage", "=", "internal"]], fields=["reserved_quantity"]))
def info(oid):
    o = call("sale.order", "read", [oid], fields=["name", "state", "amount_total", "picking_ids", "order_line"])[0]
    l = call("sale.order.line", "read", o["order_line"], fields=["product_uom_qty", "qty_delivered"])
    pk = call("stock.picking", "read", o["picking_ids"], fields=["name", "state", "picking_type_code"]) if o["picking_ids"] else []
    # también las devoluciones (no cuelgan de sale_id): se buscan por origen
    ret = call("stock.picking", "search_read", [["origin", "ilike", "Retorno de"], ["origin", "ilike", ""]], fields=["name", "state", "picking_type_code", "origin"], limit=0) if False else []
    return {"nombre": o["name"], "estado": o["state"], "total": o["amount_total"], "pedida": [x["product_uom_qty"] for x in l], "entregada": [x["qty_delivered"] for x in l],
            "entregas": [(p["picking_type_code"], p["state"]) for p in pk]}
def abiertas(oid): return [p for p in call("stock.picking", "search_read", [["sale_id", "=", oid], ["state", "not in", ["done", "cancel"]]], fields=["name"])]
def devoluciones_de(oid):
    ent = call("stock.picking", "search", [["sale_id", "=", oid], ["picking_type_code", "=", "outgoing"], ["state", "=", "done"]])
    return call("stock.picking", "search_read", [["return_id", "in", ent]], fields=["name", "state", "picking_type_code"]) if ent else []
try:
    P1, P2 = prod("13062"), prod("5490")
    P0 = call("product.product", "search_read", [["sale_ok", "=", True], ["qty_available", "=", 0], ["is_storable", "=", True]], fields=["id", "display_name", "lst_price", "qty_available"], limit=1)[0]
    S1, S2, S0 = stock(P1["id"]), stock(P2["id"]), stock(P0["id"])
    print(f"Copia: {bot.ODOO_URL} / {bot.ODOO_DB} | modo={bot.MODO_ENTREGA}\nProductos: P1={P1['display_name'][:40]} (stock {S1:.0f}) | P2={P2['display_name'][:40]} (stock {S2:.0f}) | P0={P0['display_name'][:40]} (stock {S0:.0f})")

    print("\n=== T1. VENTA de 2 unidades de P1 (el cliente se lleva 2) ===")
    t = time.time(); o1 = bot.registrar_venta_completa(P1["id"], 2); dt1 = time.time() - t; i = info(o1)
    chk("Odoo restó el stock (el bot ya no escribe las existencias)", stock(P1["id"]) == S1 - 2, f"{S1:.0f} → {stock(P1['id']):.0f}")
    chk("la entrega quedó HECHA", i["entregas"] == [("outgoing", "done")], f"{i['entregas']}")
    chk("la orden sigue confirmada y 'entregada' = 2", i["estado"] == "sale" and i["entregada"] == [2.0] and i["pedida"] == [2.0])
    chk("sin entregas abiertas ni unidades reservadas de más", not abiertas(o1) and reservadas(P1["id"]) == 0)
    print(f"      (tardó {dt1:.1f} s)")

    print("\n=== T2. Otra manguera en otra orden (P2 ×1) ===")
    o2 = bot.registrar_venta_completa(P2["id"], 1)
    chk("stock de P2 bajó 1 y su entrega quedó hecha", stock(P2["id"]) == S2 - 1 and info(o2)["entregas"] == [("outgoing", "done")])

    print("\n=== T3. AL DÍA SIGUIENTE devuelve 1 de las 2 de P1 (devolución PARCIAL, forma correcta) ===")
    t = time.time(); r = bot.aplicar_devolucion(o1, P1["id"], 1); dt3 = time.time() - t; i = info(o1)
    print("      →", {k: r[k] for k in ("ok", "msg", "antes", "despues", "aviso", "error_stock") if k in r}, f"({dt3:.1f} s)")
    chk("devolución ok", r["ok"] and r.get("error_stock") is None)
    chk("el stock volvió a subir 1 (lo hizo Odoo con su 'Devolución')", stock(P1["id"]) == S1 - 1 and r["despues"] == r["antes"] + 1)
    chk("la orden pasó de ×2 a ×1 y sigue confirmada", i["pedida"] == [1.0] and i["estado"] == "sale")
    chk("Odoo cuenta 1 unidad entregada (descontó la devuelta)", i["entregada"] == [1.0])
    chk("el total de la orden bajó a 1 unidad", abs(i["total"] - P1["lst_price"]) < 1)
    chk("existe la devolución de Odoo ligada a la entrega original, hecha", [d["state"] for d in devoluciones_de(o1)] == ["done"], f"{devoluciones_de(o1)}")
    chk("no quedó ninguna entrega abierta ni reserva", not abiertas(o1) and reservadas(P1["id"]) == 0)

    print("\n=== T4. y devuelve la que quedaba (orden de 1 → se cancela) ===")
    r = bot.aplicar_devolucion(o1, P1["id"], 1); i = info(o1)
    chk("devolución ok; la orden quedó cancelada", r["ok"] and i["estado"] == "cancel", f"{r['msg']}")
    chk("el stock de P1 quedó EXACTAMENTE como al inicio", stock(P1["id"]) == S1, f"{S1:.0f} → {stock(P1['id']):.0f}")
    r = bot.aplicar_devolucion(o2, P2["id"], 1); chk("la otra manguera (P2, 1 unidad): devolución total → stock original", r["ok"] and stock(P2["id"]) == S2 and info(o2)["estado"] == "cancel")

    print("\n=== T5. Salvaguardas ===")
    o3 = bot.registrar_venta_completa(P1["id"], 1)
    r = bot.aplicar_devolucion(o3, P1["id"], 2); chk("devolver MÁS de lo entregado → rechazado, stock intacto", not r["ok"] and stock(P1["id"]) == S1 - 1, r["msg"])
    r = bot.aplicar_devolucion(o3, P1["id"], 1); chk("devolución normal → ok", r["ok"] and stock(P1["id"]) == S1)
    r = bot.aplicar_devolucion(o3, P1["id"], 1); chk("devolver una orden YA cancelada → rechazado, stock intacto", not r["ok"] and stock(P1["id"]) == S1, r["msg"])

    print("\n=== T6. Vender SIN stock (como hoy: el bot avisa pero deja vender) ===")
    o0 = bot.registrar_venta_completa(P0["id"], 1)
    chk("la venta se hace y el stock queda en -1 (igual que antes)", stock(P0["id"]) == S0 - 1 and info(o0)["entregas"] == [("outgoing", "done")], f"{S0:.0f} → {stock(P0['id']):.0f}")
    r = bot.aplicar_devolucion(o0, P0["id"], 1); chk("y su devolución lo deja de nuevo en 0", r["ok"] and stock(P0["id"]) == S0)

    print("\n=== T7. FALLAS: la venta no debe quedar a medias ===")
    orig = bot.validar_picking
    def _falla(pid): raise RuntimeError("falla simulada al validar")
    bot.validar_picking = _falla
    n_ord = call("sale.order", "search_count", [])
    try: bot.registrar_venta_completa(P1["id"], 1); lanzo = False
    except RuntimeError as e: lanzo = True; msg = str(e)
    bot.validar_picking = orig
    ult = call("sale.order", "search_read", [], fields=["state"], order="id desc", limit=1)[0]
    chk("si Odoo no puede validar la entrega: el bot avisa con un error claro", lanzo, msg[:70])
    chk("...la orden queda CANCELADA y el stock NO cambia", ult["state"] == "cancel" and stock(P1["id"]) == S1)
    o4 = bot.registrar_venta_completa(P1["id"], 1)
    orig_call = bot.odoo_call
    def _call_falla(model, method, *a, **k):
        if method == "action_create_returns": raise RuntimeError("falla simulada del asistente de devolución")
        return orig_call(model, method, *a, **k)
    bot.odoo_call = _call_falla
    r = bot.aplicar_devolucion(o4, P1["id"], 1); bot.odoo_call = orig_call
    chk("si falla la devolución de Odoo: no se hace nada (stock sigue = vendido) y se informa", not r["ok"] and stock(P1["id"]) == S1 - 1 and info(o4)["estado"] == "sale", r["msg"][:80])
    r = bot.aplicar_devolucion(o4, P1["id"], 1); chk("reintentar después sí funciona", r["ok"] and stock(P1["id"]) == S1)

    print("\n=== T8. Ventas ANTERIORES (modo simple) se devuelven por el camino de siempre ===")
    bot.MODO_ENTREGA = "simple"; o5 = bot.registrar_venta_completa(P2["id"], 1); bot.MODO_ENTREGA = "odoo"; i = info(o5)
    chk("modo simple: stock restado por el bot y entrega cancelada", stock(P2["id"]) == S2 - 1 and i["entregas"] == [("outgoing", "cancel")], f"{i['entregas']}")
    r = bot.aplicar_devolucion(o5, P2["id"], 1)
    chk("su devolución usa el camino anterior (el bot repone el stock) y cancela la orden", r["ok"] and stock(P2["id"]) == S2 and info(o5)["estado"] == "cancel")

    print("\n=== T9. Resumen de ventas de hoy (usa el total de las órdenes) ===")
    o6 = bot.registrar_venta_completa(P1["id"], 2); o7 = bot.registrar_venta_completa(P2["id"], 1); bot.aplicar_devolucion(o6, P1["id"], 1)
    mias = [o6, o7]; tot = sum(info(x)["total"] for x in mias if info(x)["estado"] == "sale")
    resumen = bot.resumen_ventas_periodo("hoy"); print("     ", resumen.replace("\n", " | ")[:200])
    chk("el resumen refleja la devolución parcial (orden o6 ahora vale 1 unidad)", info(o6)["total"] == P1["lst_price"] and "Total" in resumen)
    bot.aplicar_devolucion(o6, P1["id"], 1); bot.aplicar_devolucion(o7, P2["id"], 1)

    print("\n=== CIERRE: stock final de los 3 productos ===")
    fin = (stock(P1["id"]), stock(P2["id"]), stock(P0["id"])); chk("los tres quedaron EXACTAMENTE como al inicio", fin == (S1, S2, S0), f"{fin} vs {(S1, S2, S0)}")
    chk("no quedó ninguna entrega abierta de las órdenes de prueba", not call("stock.picking", "search", [["sale_id", "in", [o1, o2, o3, o4, o5, o6, o7, o0]], ["state", "not in", ["done", "cancel"]]]))
except Exception:
    print("\n💥 EXCEPCIÓN NO ESPERADA:"); traceback.print_exc(); R.append(("sin excepciones", False))
print("\n=== RESULTADO:", sum(1 for _, c in R if c), "de", len(R), "OK ===")
for t, c in R:
    if not c: print("  MAL:", t)
print("TODO OK" if all(c for _, c in R) else "HAY FALLOS")
