# Prueba de `actualizar_producto(..., nuevo_costo=)` contra la COPIA temporal (puerto 8070): el costo se guarda en la VARIANTE exacta,
# no toca hermanas ni el precio, y no crea asientos contables ni movimientos de stock.
# Primero:  ./copia_odoo.sh arrancar   Después:  python3 probar_costo_en_copia.py   Al terminar:  ./copia_odoo.sh parar
import os, sys, traceback
os.environ["ODOO_URL"] = "http://127.0.0.1:8070"; os.environ["ODOO_DB"] = "radibombas_prueba"; os.environ["MODO_ENTREGA"] = "odoo"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
import logging; logging.disable(logging.CRITICAL)
import bot
assert bot.ODOO_DB == "radibombas_prueba" and bot.ODOO_URL.endswith(":8070"), "¡NO está apuntando a la copia!"
R = []
def chk(t, c, extra=""): R.append((t, bool(c))); print(f"  {'OK ' if c else 'MAL'} {t} {extra}")
uid, models = bot.odoo_connect()
def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)
def prod(cod): return call("product.product", "search_read", [["default_code", "=", cod]], fields=["id", "display_name", "lst_price", "standard_price", "qty_available", "product_tmpl_id"], limit=1)[0]
def cuentas():
    return {"asientos": call("account.move", "search_count", []), "mov_stock": call("stock.move", "search_count", []), "valores": call("product.value", "search_count", [])}
try:
    V = prod("1237"); hermanas = call("product.product", "search_read", [["product_tmpl_id", "=", V["product_tmpl_id"][0]], ["id", "!=", V["id"]]], fields=["id", "standard_price", "lst_price"])
    C0 = cuentas()
    print(f"Copia: {bot.ODOO_URL} | producto {V['display_name']} (id {V['id']}, costo {V['standard_price']}, precio {V['lst_price']}) con {len(hermanas)} variantes hermanas | {C0}")

    print("\n=== T1. Costo en una variante de una plantilla con 13 variantes (el caso real)")
    bot.actualizar_producto(V["id"], nuevo_costo=78075)
    d = prod("1237")
    chk("la variante 1237 quedó con costo 78075", d["standard_price"] == 78075, str(d["standard_price"]))
    chk("el precio de venta NO cambió", d["lst_price"] == V["lst_price"], f"{d['lst_price']}")
    h2 = call("product.product", "read", [h["id"] for h in hermanas], fields=["standard_price", "lst_price"])
    chk("las 12 variantes hermanas NO cambiaron (costo ni precio)", all(a["standard_price"] == b["standard_price"] and a["lst_price"] == b["lst_price"] for a, b in zip(sorted(hermanas, key=lambda x: x["id"]), sorted(h2, key=lambda x: x["id"]))))
    C1 = cuentas()
    chk("sin asientos contables ni movimientos de stock nuevos", C1["asientos"] == C0["asientos"] and C1["mov_stock"] == C0["mov_stock"], f"{C0} → {C1}")
    print(f"      (registro de historial de costos 'product.value': {C0['valores']} → {C1['valores']})")
    chk("el stock no cambió", prod("1237")["qty_available"] == V["qty_available"])

    print("\n=== T2. Producto con una sola variante: el costo también queda en la plantilla")
    P = prod("5490"); t0 = call("product.template", "read", [P["product_tmpl_id"][0]], fields=["standard_price", "list_price"])[0]
    bot.actualizar_producto(P["id"], nuevo_costo=12345.5)
    d = prod("5490"); t1 = call("product.template", "read", [P["product_tmpl_id"][0]], fields=["standard_price", "list_price"])[0]
    chk("variante y plantilla con 12345.5 (admite decimales)", d["standard_price"] == 12345.5 and t1["standard_price"] == 12345.5, f"{d['standard_price']} / {t1['standard_price']}")
    chk("el precio de lista no cambió", t1["list_price"] == t0["list_price"])

    print("\n=== T3. Costo 0 y volver al valor anterior")
    bot.actualizar_producto(P["id"], nuevo_costo=0); chk("costo 0 se guarda", prod("5490")["standard_price"] == 0)
    bot.actualizar_producto(P["id"], nuevo_costo=P["standard_price"]); chk("se restablece el original", prod("5490")["standard_price"] == P["standard_price"], str(P["standard_price"]))

    print("\n=== T4. Precio + costo + stock juntos (una sola actualización) y el mensaje final")
    antes = prod("5490"); mv0 = call("stock.move", "search_count", [])
    bot.limpiar_pendiente = lambda u: None
    msg = bot.ejecutar_actualizacion("u", {"actualizaciones": [{"product_id": P["id"], "product_name": P["display_name"], "cambios": {"nuevo_precio": antes["lst_price"], "nuevo_costo": 9999, "agregar_stock": 2}}]})
    d = prod("5490")
    chk("costo 9999 y stock +2", d["standard_price"] == 9999 and d["qty_available"] == antes["qty_available"] + 2, f"stock {antes['qty_available']:.0f} → {d['qty_available']:.0f}")
    chk("el mensaje dice 'Costo: $9,999' y el stock", "Costo: $9,999" in msg and f"Stock: {int(d['qty_available'])}" in msg, msg.replace("\n", " | "))
    chk("el stock dejó UN movimiento de inventario (+2) y el costo ninguno", call("stock.move", "search_count", []) == mv0 + 1)
    bot.actualizar_producto(P["id"], nuevo_costo=P["standard_price"], establecer_stock=int(antes["qty_available"]))   # deja la copia como estaba

    print("\n=== T5. Una falla se reporta, no se traga")
    msg = bot.ejecutar_actualizacion("u", {"actualizaciones": [{"product_id": 99999999, "product_name": "[X] inexistente", "cambios": {"nuevo_costo": 5}}]})
    chk("producto inexistente → ❌ con 'Revísalo en Odoo'", "❌" in msg and "Revísalo en Odoo" in msg, msg.replace("\n", " | ")[:120])
    C2 = cuentas()
    chk("al final: 0 asientos contables nuevos en toda la prueba", C2["asientos"] == C0["asientos"])
except Exception:
    traceback.print_exc(); R.append(("EXCEPCIÓN en la prueba", False))
malos = [t for t, c in R if not c]
print(f"\n{len(R) - len(malos)}/{len(R)} comprobaciones OK" + ("" if not malos else "  ← FALLARON: " + "; ".join(malos)))
sys.exit(1 if malos else 0)
