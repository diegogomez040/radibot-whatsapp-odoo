# Prueba COMPLETA de la migración de las corrugadas contra la COPIA temporal (puerto 8070): migra, verifica y prueba el bot sobre lo migrado.
# Primero:  ./copia_odoo.sh arrancar   Después:  python3 probar_migracion_corrugadas_en_copia.py   Al terminar:  ./copia_odoo.sh parar
import os, sys, json, time, traceback
from pathlib import Path
os.environ["ODOO_URL"] = "http://127.0.0.1:8070"; os.environ["ODOO_DB"] = "radibombas_prueba"; os.environ["MODO_ENTREGA"] = "odoo"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "pruebas"))
import logging; logging.disable(logging.CRITICAL)
import bot, migrar_corrugadas
assert bot.ODOO_DB == "radibombas_prueba" and bot.ODOO_URL.endswith(":8070"), "¡NO está apuntando a la copia!"
LOGMEM = []
CAMPOS_COMPARAR = ["categ_id", "type", "is_storable", "tracking", "uom_id", "taxes_id", "supplier_taxes_id", "sale_ok", "purchase_ok", "invoice_policy"]
bot.cargar_ventas_log = lambda: LOGMEM; bot.guardar_ventas_log = lambda d: None
FOTO, MAPA = "/tmp/migracion_corrugadas_test.json", "/tmp/mapa_variantes_test.json"
for f in (FOTO, MAPA): Path(f).unlink(missing_ok=True)
bot.MAPA_VARIANTES_FILE = Path(MAPA)
R = []
def chk(t, c, extra=""): R.append((t, bool(c))); print(f"  {'OK ' if c else 'MAL'} {t} {extra}")
uid, models = bot.odoo_connect()
def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)
def stock_quant(pid): return bot.cantidad_en_bodega(pid)
def total_bodega(): return sum(q["quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["quantity"]))
def abiertas(): return call("stock.picking", "search_count", [["state", "in", ["draft", "waiting", "confirmed", "assigned"]]])
def registrar_en_log(oid, pid, nombre, cant, antes):
    LOGMEM.append({"usuario": "u", "timestamp": bot.ahora_utc().isoformat(), "product_id": pid, "product_name": nombre, "cantidad": cant, "orden_id": oid, "stock_anterior": antes, "deshecha": False})
try:
    tid = call("product.template", "search", [["name", "=", "Manguera corrugada"], ["categ_id", "=", 88], ["active", "=", True]])[0]
    viejas = call("product.product", "search_read", [["product_tmpl_id", "=", tid]], fields=["id", "display_name", "lst_price", "qty_available"])
    con_stock = sorted([v for v in viejas if v["qty_available"] >= 3], key=lambda v: v["id"])
    A, B = con_stock[0], con_stock[1]   # A: venta "de odoo" antes de migrar; B: venta estilo "modo simple" antes de migrar
    lineas_ventas_antes = call("sale.order.line", "search_count", [["product_id", "in", [v["id"] for v in viejas]]])
    print(f"Copia lista: {len(viejas)} variantes viejas. A={A['display_name']} (stock {A['qty_available']:.0f}), B={B['display_name']} (stock {B['qty_available']:.0f})")

    print("\n=== P. ANTES de migrar: dos ventas con las variantes viejas")
    sA = stock_quant(A["id"]); oA = bot.registrar_venta_completa(A["id"], 1); registrar_en_log(oA, A["id"], A["display_name"], 1, sA)
    bot.MODO_ENTREGA = "simple"; sB = stock_quant(B["id"]); oB = bot.registrar_venta_completa(B["id"], 1); registrar_en_log(oB, B["id"], B["display_name"], 1, sB); bot.MODO_ENTREGA = "odoo"
    chk("venta A (modo odoo, entrega hecha) y venta B (modo simple, entrega cancelada) hechas", stock_quant(A["id"]) == sA - 1 and stock_quant(B["id"]) == sB - 1)
    total_antes = total_bodega(); asientos_antes = call("account.move", "search_count", [])

    print("\n=== M. La migración")
    t0 = time.time(); res = migrar_corrugadas.migrar(bot, FOTO, MAPA); dt = time.time() - t0
    rs = res["resumen"]; print(f"      (tardó {dt:.0f} s)"); tid_n = res["tmpl_nuevo"]
    chk("128 variantes nuevas activas, en UNA sola plantilla activa 'Manguera corrugada'", rs["variantes_nuevas"] == 128 and rs["plantillas_activas_con_ese_nombre"] == 1 and tid_n != tid)
    chk("las 128 variantes VIEJAS siguen existiendo, TODAS archivadas (ninguna se borró)", rs["viejas_archivadas"] == 128 and rs["viejas_eliminadas"] == 0, f"archivadas={rs['viejas_archivadas']}, eliminadas={rs['viejas_eliminadas']}")
    vieja = call("product.template", "read", [tid], fields=["name", "active"], context={"active_test": False})[0]
    chk("la plantilla vieja quedó archivada y renombrada '(versión anterior)'", not vieja["active"] and "versión anterior" in vieja["name"], vieja["name"])
    chk("el stock de las viejas quedó en 0 y el de las nuevas es el que había", rs["stock_viejas"] == 0 and rs["stock_nuevas"] == rs["stock_antes"] and rs["stock_por_medida_igual"], f"{rs['stock_antes']:.0f} → {rs['stock_nuevas']:.0f}")
    chk("el total de la bodega no cambió", abs(total_bodega() - total_antes) < 1e-6, f"{total_antes:.0f} → {total_bodega():.0f}")
    chk("0 asientos contables y 0 operaciones abiertas", call("account.move", "search_count", []) == asientos_antes and abiertas() == 0)
    nuevas = call("product.product", "search_read", [["product_tmpl_id", "=", tid_n]], fields=["id", "display_name", "lst_price", "product_template_attribute_value_ids"])
    foto = {f["nombre"]: f for f in res["foto"]}
    chk("los 128 NOMBRES son idénticos a los de antes ('Manguera corrugada (2, 26)')", {v["display_name"] for v in nuevas} == set(foto), "")
    chk("los 128 PRECIOS son idénticos a los de antes", all(abs(v["lst_price"] - foto[v["display_name"]]["precio"]) < 0.005 for v in nuevas))
    chk("precio base de la plantilla nueva en 0, y mismos ajustes que la vieja (categoría, tipo, impuestos)", call("product.template", "read", [tid_n], fields=["list_price"])[0]["list_price"] == 0 and call("product.template", "read", [tid_n], fields=CAMPOS_COMPARAR)[0] == {**call("product.template", "read", [tid], fields=CAMPOS_COMPARAR, context={"active_test": False})[0], "id": tid_n})
    chk("las 88+ líneas de venta históricas siguen apuntando a las viejas (historial intacto)", call("sale.order.line", "search_count", [["product_id", "in", [v["id"] for v in viejas]]]) >= lineas_ventas_antes)
    mapa = bot.variante_vigente  # se carga el archivo del mapa
    chk("el mapa viejo→nuevo está cargado en el bot", bot.variante_vigente(A["id"]) != A["id"] and bot.variante_vigente(A["id"]) == res["mapa"][str(A["id"])])
    nuevoA, nuevoB = bot.variante_vigente(A["id"]), bot.variante_vigente(B["id"])

    print("\n=== S. La búsqueda del bot sobre lo migrado")
    def busca(attrs, query="manguera corrugada", n=20): return [p["display_name"] for p in bot.buscar_productos(query, atributos=attrs, categoria="mangueras corrugadas", limit=n, relajar_categoria=False)]
    r = busca(["2", "26"]); chk("['2','26'] → exactamente 'Manguera corrugada (2, 26)'", r == ["Manguera corrugada (2, 26)"], str(r))
    r = busca(["1 1/2", "24"]); chk("['1 1/2','24'] → solo esa", r == ["Manguera corrugada (1 1/2, 24)"], str(r))
    r = busca(["1", "12"]); chk("['1','12'] → solo (1, 12), no (1 1/4, 12) ni (1, 14)", r == ["Manguera corrugada (1, 12)"], str(r))
    r = busca(["2"], n=40); chk("['2'] (solo diámetro) → las 16 de diámetro 2, y ninguna de 2 1/4, 2 1/2 o 2 3/4", len(r) == 16 and all(x.startswith("Manguera corrugada (2, ") for x in r), f"{len(r)}")
    r = busca(["30"], n=40); chk("['30'] (solo largo) → las 8 de largo 30", len(r) == 8 and all(x.endswith(", 30)") for x in r), f"{len(r)}")
    r = [p["display_name"] for p in bot.buscar_productos("corrugada", atributos=["2 1/2", "36"], limit=5)]; chk("sin categoría también", r == ["Manguera corrugada (2 1/2, 36)"], str(r))

    print("\n=== V. Venta y precio individual sobre lo migrado")
    v = next(p for p in nuevas if p["display_name"] == "Manguera corrugada (1, 42)"); otros = {p["id"]: p["lst_price"] for p in nuevas if p["id"] != v["id"]}
    plan = bot.plan_precio(v["id"], 30000); chk("precio de UNA corrugada ahora SÍ se puede (modo 'extra')", plan.get("modo") == "extra", str(plan.get("modo") or plan.get("error"))[:60])
    antes_v = v["lst_price"]; bot.actualizar_producto(v["id"], nuevo_precio=30000)
    desp = {p["id"]: p["lst_price"] for p in call("product.product", "search_read", [["product_tmpl_id", "=", tid_n]], fields=["lst_price"])}
    chk("la (1, 42) cambió a $30,000 y las otras 127 quedaron IGUAL", desp[v["id"]] == 30000 and all(desp[i] == p for i, p in otros.items()), f"{antes_v:,.0f} → {desp[v['id']]:,.0f}")
    bot.actualizar_producto(v["id"], nuevo_precio=antes_v)
    v2 = next(p for p in nuevas if p["display_name"] == "Manguera corrugada (2, 12)"); sv = stock_quant(v2["id"]); ov = bot.registrar_venta_completa(v2["id"], 1)
    chk("una venta nueva descuenta de la variante NUEVA", stock_quant(v2["id"]) == sv - 1)

    print("\n=== D. Devoluciones de ventas hechas ANTES de migrar")
    sn = stock_quant(nuevoA); so = stock_quant(A["id"])
    r = bot.aplicar_devolucion(oA, A["id"], 1)
    chk("venta A (entrega hecha, variante archivada): devolución aceptada", r["ok"] and not r["error_stock"], f"{r['msg']}" if r["ok"] else f"FALLÓ: {r['msg']}")
    chk("el stock vuelve a la variante NUEVA (+1) y la vieja queda en 0", stock_quant(nuevoA) == sn + 1 and stock_quant(A["id"]) == so, f"nueva {sn:.0f}→{stock_quant(nuevoA):.0f}, vieja {so:.0f}→{stock_quant(A['id']):.0f}")
    chk("el mensaje muestra el stock de la NUEVA ('Stock: a → b')", r["ok"] and r["antes"] == sn and r["despues"] == sn + 1, f"{r.get('antes')} → {r.get('despues')}")
    sn2 = stock_quant(nuevoB); so2 = stock_quant(B["id"])
    r = bot.aplicar_devolucion(oB, B["id"], 1)
    chk("venta B (entrega cancelada, camino manual): aceptada", r["ok"] and not r["error_stock"], r["msg"] if r["ok"] else f"FALLÓ: {r['msg']}")
    chk("y el stock vuelve a la variante NUEVA (+1), la vieja sigue en 0", stock_quant(nuevoB) == sn2 + 1 and stock_quant(B["id"]) == so2, f"nueva {sn2:.0f}→{stock_quant(nuevoB):.0f}")
    # por el flujo del chat: lista de devoluciones y vista previa con el stock de la vigente
    LOGMEM.clear(); registrar_en_log(ov, v2["id"], v2["display_name"], 1, sv)
    bot.obtener_pendiente = lambda u: None; guardado = {}; bot.guardar_pendiente = lambda u, a: guardado.update(a); bot.limpiar_pendiente = lambda u: None
    r = bot.handler_devolver("u", {"producto_query": "manguera corrugada (2, 12)"})
    chk("el chat arma la devolución de una venta NUEVA con su stock", guardado.get("items") and guardado["items"][0]["stock"] == stock_quant(v2["id"]), str([i["stock"] for i in guardado.get("items", [])]))

    print("\n=== C. Con el MODELO real, sobre lo migrado (frases como las de Diego)")
    import copy as _copy
    mem = {}
    bot.obtener_pendiente = lambda u: _copy.deepcopy(mem.get(u)); bot.guardar_pendiente = lambda u, a: mem.__setitem__(u, _copy.deepcopy(a)); bot.limpiar_pendiente = lambda u: mem.pop(u, None)
    def dice(frase, n=3, hist=None):
        out = []
        for _ in range(n):
            mem.clear(); r, _h = bot.procesar_con_openai("u", frase, _copy.deepcopy(hist or [])); out.append((r, _copy.deepcopy(mem.get("u"))))
        return out
    o = dice("¿tienes manguera corrugada de 2 por 26?"); chk("'manguera corrugada de 2 por 26' → encuentra (2, 26)", all("(2, 26)" in r and "(2, 24)" not in r for r, _ in o), f"{sum('(2, 26)' in r for r, _ in o)}/3")
    o = dice("hay corrugada de 1 1/2 por 24"); chk("'corrugada de 1 1/2 por 24' → (1 1/2, 24)", all("(1 1/2, 24)" in r for r, _ in o), f"{sum('(1 1/2, 24)' in r for r, _ in o)}/3")
    o = dice("se vendió una corrugada de 2 por 30"); chk("'se vendió una corrugada de 2 por 30' → vista previa de venta de esa medida", all("(2, 30)" in r and "¿Confirmas" in r for r, _ in o), f"{sum('(2, 30)' in r and '¿Confirmas' in r for r, _ in o)}/3")
    o = dice("el precio de la manguera corrugada de 1 por 42 es 30000"); chk("precio de UNA corrugada → vista previa solo de esa (sin avisos de grupo)", all("(1, 42)" in r and "Precio:" in r and "solo esta variante" in r and "¿Confirmas" in r for r, _ in o), f"{sum('solo esta variante' in r for r, _ in o)}/3")
    print("      " + o[0][0].replace("\n", "\n      "))
    o = dice("actualiza el costo de la corrugada de 2 por 26 a 15000"); chk("costo de una corrugada → solo esa medida", all("(2, 26)" in r and "Costo: $0 → $15,000" in r for r, _ in o), f"{sum('Costo: $0 → $15,000' in r for r, _ in o)}/3")

    print("\n=== I. Falta un dato (modelo real, estructura migrada)")
    def conv2(mensajes, n=3):
        res_ = []
        for _ in range(n):
            mem.clear(); bot.MEDIDA_PREGUNTADA.clear(); hist = []; out = []
            for m in mensajes:
                r, hist = bot.procesar_con_openai("u", m, hist); out.append((r, _copy.deepcopy(mem.get("u"))))
            res_.append(out)
        return res_
    r = conv2(["se vendió una manguera corrugada de 40"]); chk("'de 40' → tabla de los 8 diámetros + pregunta", all("• 2 3/4:" in x[0][0] and "¿Qué diámetro necesitas?" in x[0][0] for x in r), f"{sum('¿Qué diámetro' in x[0][0] for x in r)}/3")
    r = conv2(["se vendió una manguera corrugada de 40", "la de dos y media"]); chk("…y 'la de dos y media' → venta de (2 1/2, 40)", all("(2 1/2, 40)" in x[1][0] and "¿Confirmas" in x[1][0] for x in r), f"{sum('(2 1/2, 40)' in x[1][0] for x in r)}/3")
    r = conv2(["manguera corrugada de 2 y media"]); chk("'de 2 y media' → tabla de los 16 largos", all("• 12:" in x[0][0] and "• 42:" in x[0][0] for x in r), f"{sum('• 42:' in x[0][0] for x in r)}/3")
    r = conv2(["manguera corrugada de 2 y media", "la de 30"]); chk("…y 'la de 30' (venta) → (2 1/2, 30)", all("(2 1/2, 30)" in x[1][0] for x in r), f"{sum('(2 1/2, 30)' in x[1][0] for x in r)}/3")

    print("\n=== G. Invariantes finales")
    chk("0 asientos contables nuevos en toda la prueba", call("account.move", "search_count", []) == asientos_antes)
    chk("sin operaciones abiertas ni unidades reservadas", abiertas() == 0 and sum(q["reserved_quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["reserved_quantity"])) == 0)
except Exception:
    traceback.print_exc(); R.append(("EXCEPCIÓN en la prueba", False))
malos = [t for t, c in R if not c]
print(f"\n{len(R) - len(malos)}/{len(R)} comprobaciones OK" + ("" if not malos else "  ← FALLARON: " + "; ".join(malos)))
sys.exit(1 if malos else 0)
