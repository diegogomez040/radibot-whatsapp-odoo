# Prueba de `ajustar_stock` con movimiento de inventario ("Cantidad de producto actualizada") contra la COPIA temporal (puerto 8070).
# Primero:  ./copia_odoo.sh arrancar   Después:  python3 probar_ajuste_inventario_en_copia.py   Al terminar:  ./copia_odoo.sh parar
# Aborta si el bot no está apuntando a la copia. No toca data/ (el registro de ventas se simula en memoria).
import os, sys, time, traceback
# --- SEGURIDAD: SOLO contra la copia temporal ---
os.environ["ODOO_URL"] = "http://127.0.0.1:8070"; os.environ["ODOO_DB"] = "radibombas_prueba"; os.environ["MODO_ENTREGA"] = "odoo"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
import logging; logging.disable(logging.CRITICAL)
import bot
assert bot.ODOO_DB == "radibombas_prueba" and bot.ODOO_URL.endswith(":8070"), "¡NO está apuntando a la copia!"
LOGMEM = []
bot.cargar_ventas_log = lambda: LOGMEM; bot.guardar_ventas_log = lambda d: None
R = []
def chk(t, c, extra=""): R.append((t, bool(c))); print(f"  {'OK ' if c else 'MAL'} {t} {extra}")
uid, models = bot.odoo_connect()
def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)
def prod(cod): return call("product.product", "search_read", [["default_code", "=", cod]], fields=["id", "display_name", "qty_available"], limit=1)[0]
def stock(pid): return call("product.product", "read", [pid], fields=["qty_available"])[0]["qty_available"]
_uso = {}
def uso(lid):
    if lid not in _uso: _uso[lid] = call("stock.location", "read", [lid], fields=["usage"])[0]["usage"]
    return _uso[lid]
def movs(pid):
    return call("stock.move", "search_read", [["product_id", "=", pid], ["state", "=", "done"]], fields=["id", "reference", "quantity", "location_id", "location_dest_id", "is_inventory", "create_uid"], order="id asc")
def nuevos(pid, desde_id):
    return [m for m in movs(pid) if m["id"] > desde_id]
def ultimo_id(pid):
    m = movs(pid); return m[-1]["id"] if m else 0
def firmado(m): return (m["quantity"] if uso(m["location_dest_id"][0]) == "internal" else 0) - (m["quantity"] if uso(m["location_id"][0]) == "internal" else 0)
def es_ajuste(m): return m["is_inventory"] and "Cantidad de producto actualizada" in (m["reference"] or "")
def global_():
    n_asientos = call("account.move", "search_count", [])
    capas = call("stock.valuation.layer", "search_count", []) if call("ir.model", "search_count", [["model", "=", "stock.valuation.layer"]]) else -1
    abiertas = call("stock.picking", "search_count", [["state", "in", ["draft", "waiting", "confirmed", "assigned"]]])
    reservado = sum(q["reserved_quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["reserved_quantity"]))
    return {"asientos": n_asientos, "capas": capas, "abiertas": abiertas, "reservado": reservado}

try:
    G0 = global_()
    A = prod("5490")
    print(f"Copia: {bot.ODOO_URL} / {bot.ODOO_DB}\nProducto A: {A['display_name'][:50]} (stock {stock(A['id']):.0f}) | estado global inicial: {G0}")

    A_s_ini, A_u_ini = stock(A["id"]), ultimo_id(A["id"])
    print("\n=== T1. Existe registro de existencias: SUBIR el stock ===")
    s0 = stock(A["id"]); u = ultimo_id(A["id"])
    bot.ajustar_stock(A["id"], s0 + 3); n = nuevos(A["id"], u)
    chk("el stock quedó en lo pedido", stock(A["id"]) == s0 + 3, f"{s0:.0f} → {stock(A['id']):.0f}")
    chk("Odoo creó UN movimiento 'Cantidad de producto actualizada' de +3 (de Inventory adjustment a bodega)", len(n) == 1 and es_ajuste(n[0]) and firmado(n[0]) == 3 and uso(n[0]["location_id"][0]) == "inventory", str([(m["reference"][:32], firmado(m)) for m in n]))
    chk("el movimiento queda a nombre del usuario del bot", n and n[0]["create_uid"], str(n[0]["create_uid"]) if n else "")

    print("\n=== T2. BAJAR el stock ===")
    u = ultimo_id(A["id"]); s1 = stock(A["id"]); bot.ajustar_stock(A["id"], s1 - 2); n = nuevos(A["id"], u)
    chk("stock correcto", stock(A["id"]) == s1 - 2)
    chk("movimiento de -2 hacia 'Inventory adjustment'", len(n) == 1 and es_ajuste(n[0]) and firmado(n[0]) == -2 and uso(n[0]["location_dest_id"][0]) == "inventory")

    print("\n=== T3. Dejarlo en el MISMO valor (no debe crear movimiento ni fallar) ===")
    u = ultimo_id(A["id"]); s2 = stock(A["id"]); r = bot.ajustar_stock(A["id"], s2); n = nuevos(A["id"], u)
    chk("sin movimiento nuevo y sin error", r is True and not n and stock(A["id"]) == s2)

    print("\n=== T4. Dejarlo en 0 ===")
    u = ultimo_id(A["id"]); s3 = stock(A["id"]); bot.ajustar_stock(A["id"], 0); n = nuevos(A["id"], u)
    chk("stock 0 con un movimiento de salida igual al que había", stock(A["id"]) == 0 and len(n) == 1 and firmado(n[0]) == -s3, f"{s3:.0f} → 0")

    print("\n=== T5. Producto NUEVO con stock inicial (no tiene registro de existencias) ===")
    tmpl = bot.crear_producto("PRUEBA copia inventario", 1000, 4, 82, "ZZ-PRUEBA-INV", 500)
    P = prod("ZZ-PRUEBA-INV"); n = movs(P["id"])
    chk("el stock inicial quedó en 4", stock(P["id"]) == 4)
    chk("y dejó su movimiento de +4", len(n) == 1 and es_ajuste(n[0]) and firmado(n[0]) == 4, str([(m["reference"][:32], firmado(m)) for m in n]))

    print("\n=== T6. actualizar_producto: sumar y dejar exacto (como 'llegaron 3' y 'el stock es 10') ===")
    u = ultimo_id(P["id"]); bot.actualizar_producto(P["id"], agregar_stock=3); n = nuevos(P["id"], u)
    chk("'llegaron 3': 4 → 7 con movimiento +3", stock(P["id"]) == 7 and len(n) == 1 and firmado(n[0]) == 3)
    u = ultimo_id(P["id"]); bot.actualizar_producto(P["id"], establecer_stock=10); n = nuevos(P["id"], u)
    chk("'el stock es 10': 7 → 10 con movimiento +3", stock(P["id"]) == 10 and len(n) == 1 and firmado(n[0]) == 3)
    bot.actualizar_producto(P["id"], nuevo_precio=1234)
    chk("cambiar solo el precio no toca el stock ni crea movimientos", stock(P["id"]) == 10 and len(movs(P["id"])) == 3)

    print("\n=== T7. Stock NEGATIVO (se vendió sin stock en el modo odoo) y luego se corrige ===")
    P0 = call("product.product", "search_read", [["sale_ok", "=", True], ["qty_available", "=", 0], ["is_storable", "=", True], ["default_code", "!=", "ZZ-PRUEBA-INV"]], fields=["id", "display_name"], limit=1)[0]
    oid = bot.registrar_venta_completa(P0["id"], 1)
    chk("la venta sin stock dejó -1", stock(P0["id"]) == -1, f"{P0['display_name'][:40]}")
    u = ultimo_id(P0["id"]); bot.actualizar_producto(P0["id"], establecer_stock=5); n = nuevos(P0["id"], u)
    chk("'el stock es 5' sobre -1 → 5 con movimiento de +6", stock(P0["id"]) == 5 and len(n) == 1 and firmado(n[0]) == 6, str([firmado(m) for m in n]))

    print("\n=== T8. reponer_stock (lo que usa una devolución del camino manual) ===")
    u = ultimo_id(P["id"]); antes, despues = bot.reponer_stock(P["id"], 2); n = nuevos(P["id"], u)
    chk("suma 2 al stock ACTUAL con su movimiento", (antes, despues) == (10, 12) and stock(P["id"]) == 12 and len(n) == 1 and firmado(n[0]) == 2, f"{antes} → {despues}")

    print("\n=== T9. Cinco ajustes seguidos del mismo producto ===")
    u = ultimo_id(P["id"]); objetivos = [20, 15, 15, 0, 8]
    for o in objetivos: bot.ajustar_stock(P["id"], o)
    n = nuevos(P["id"], u)
    chk("el stock final es el último pedido y hay un movimiento por cada cambio real (4)", stock(P["id"]) == 8 and len(n) == 4 and sum(firmado(m) for m in n) == 8 - 12, f"suma de movimientos {sum(firmado(m) for m in n):+.0f}")

    print("\n=== T10. Devolución de una venta del MODO ANTERIOR (sin entrega hecha) por el camino manual ===")
    v = call("sale.order", "read", [604], fields=["name", "state", "order_line"])[0]
    lin = call("sale.order.line", "read", v["order_line"], fields=["product_id", "product_uom_qty"])[0]
    pid4 = lin["product_id"][0]; s4 = stock(pid4); u = ultimo_id(pid4)
    r = bot.aplicar_devolucion(604, pid4, int(lin["product_uom_qty"])); n = nuevos(pid4, u)
    chk(f"{v['name']}: devolución aceptada, orden cancelada y stock +{int(lin['product_uom_qty'])}", r["ok"] and not r["error_stock"] and stock(pid4) == s4 + lin["product_uom_qty"] and call("sale.order", "read", [604], fields=["state"])[0]["state"] == "cancel", f"{r['msg']} | {r['antes']} → {r['despues']}")
    chk("y ahora SÍ deja movimiento de inventario en el historial", len(n) == 1 and es_ajuste(n[0]))

    print("\n=== T11. Invariantes de TODO el inventario ===")
    G1 = global_()
    chk("no se creó ningún asiento contable por los ajustes", G1["asientos"] == G0["asientos"], f"{G0['asientos']} → {G1['asientos']}")
    chk("ni capas de valoración", G1["capas"] == G0["capas"], f"{G0['capas']} → {G1['capas']}")
    chk("sin operaciones abiertas ni unidades reservadas", G1["abiertas"] == 0 and G1["reservado"] == 0, str(G1))
    # el cambio de stock de cada producto probado = suma de los movimientos hechos durante la prueba (el historial lo explica completo)
    movs_A = [m for m in movs(A["id"]) if m["id"] > A_u_ini]
    chk("A (5490): cambio de stock = suma de los movimientos de la prueba", abs(stock(A["id"]) - A_s_ini - sum(firmado(m) for m in movs_A)) < 1e-6, f"{stock(A['id']) - A_s_ini:+.0f} vs {sum(firmado(m) for m in movs_A):+.0f}")
    chk("producto nuevo: stock = suma de todos sus movimientos", abs(stock(P["id"]) - sum(firmado(m) for m in movs(P["id"]))) < 1e-6, f"{stock(P['id']):.0f} vs {sum(firmado(m) for m in movs(P['id'])):+.0f}")
except Exception:
    traceback.print_exc(); R.append(("EXCEPCIÓN en la prueba", False))
malos = [t for t, c in R if not c]
print(f"\n{len(R) - len(malos)}/{len(R)} comprobaciones OK" + ("" if not malos else "  ← FALLARON: " + "; ".join(malos)))
sys.exit(1 if malos else 0)
