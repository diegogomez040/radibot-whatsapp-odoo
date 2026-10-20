# Prueba COMPLETA de la migración de la "Tapa pequeña" contra la COPIA temporal (puerto 8070): migra, verifica y prueba el bot sobre lo migrado.
# Primero:  ./copia_odoo.sh arrancar   Después:  python3 probar_migracion_tapa_en_copia.py   Al terminar:  ./copia_odoo.sh parar
import os, sys, json, re, time, traceback
from pathlib import Path
os.environ["ODOO_URL"] = "http://127.0.0.1:8070"; os.environ["ODOO_DB"] = "radibombas_prueba"; os.environ["MODO_ENTREGA"] = "odoo"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "pruebas"))
import logging; logging.disable(logging.CRITICAL)
import bot, migrar_tapa_pequena
assert bot.ODOO_DB == "radibombas_prueba" and bot.ODOO_URL.endswith(":8070"), "¡NO está apuntando a la copia!"
LOGMEM = []
CAMPOS_COMPARAR = ["categ_id", "type", "is_storable", "tracking", "uom_id", "taxes_id", "supplier_taxes_id", "sale_ok", "purchase_ok", "invoice_policy"]
bot.cargar_ventas_log = lambda: LOGMEM; bot.guardar_ventas_log = lambda d: None
FOTO, MAPA = "/tmp/migracion_tapa_test.json", "/tmp/mapa_variantes_tapa_test.json"
for f in (FOTO, MAPA): Path(f).unlink(missing_ok=True)
json.dump({"999999": 888888}, open(MAPA, "w"))          # una entrada «de las corrugadas» que la migración NO debe pisar
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
    tid = call("product.template", "search", [["name", "=", "Tapa pequeña"], ["categ_id", "=", 39], ["active", "=", True]])[0]
    viejas = call("product.product", "search_read", [["product_tmpl_id", "=", tid]], fields=["id", "display_name", "lst_price", "qty_available", "default_code"])
    con_stock = sorted([v for v in viejas if v["qty_available"] >= 3], key=lambda v: v["id"])
    A, B = con_stock[0], con_stock[1]
    codigo_A = A["default_code"]
    lineas_ventas_antes = call("sale.order.line", "search_count", [["product_id", "in", [v["id"] for v in viejas]]])
    otras_plantillas = {t["id"]: t["name"] for t in call("product.template", "search_read", [["name", "in", ["Tapa mediana", "Tapa grande"]]], fields=["name"])}
    print(f"Copia lista: {len(viejas)} variantes viejas. A={A['display_name']} (stock {A['qty_available']:.0f}), B={B['display_name']} (stock {B['qty_available']:.0f})")

    print("\n=== P. ANTES de migrar: dos ventas con las variantes viejas")
    sA = stock_quant(A["id"]); oA = bot.registrar_venta_completa(A["id"], 1); registrar_en_log(oA, A["id"], A["display_name"], 1, sA)
    bot.MODO_ENTREGA = "simple"; sB = stock_quant(B["id"]); oB = bot.registrar_venta_completa(B["id"], 1); registrar_en_log(oB, B["id"], B["display_name"], 1, sB); bot.MODO_ENTREGA = "odoo"
    chk("venta A (modo odoo) y venta B (modo simple) hechas", stock_quant(A["id"]) == sA - 1 and stock_quant(B["id"]) == sB - 1)
    total_antes = total_bodega(); asientos_antes = call("account.move", "search_count", [])
    precios_otras = {p["id"]: p["lst_price"] for p in call("product.product", "search_read", [["product_tmpl_id", "in", list(otras_plantillas)]], fields=["lst_price"])}
    stock_otras = {p["id"]: p["qty_available"] for p in call("product.product", "search_read", [["product_tmpl_id", "in", list(otras_plantillas)]], fields=["qty_available"])}

    print("\n=== M. La migración")
    t0 = time.time(); res = migrar_tapa_pequena.migrar(bot, FOTO, MAPA); dt = time.time() - t0
    rs = res["resumen"]; print(f"      (tardó {dt:.0f} s)"); tid_n = res["tmpl_nuevo"]
    chk("4 variantes nuevas activas, en UNA sola plantilla activa 'Tapa pequeña'", rs["variantes_nuevas"] == 4 and rs["plantillas_activas_con_ese_nombre"] == 1 and tid_n != tid)
    chk("las 4 variantes VIEJAS siguen existiendo, TODAS archivadas (ninguna se borró)", rs["viejas_archivadas"] == 4 and rs["viejas_eliminadas"] == 0, f"archivadas={rs['viejas_archivadas']}, eliminadas={rs['viejas_eliminadas']}")
    vieja = call("product.template", "read", [tid], fields=["name", "active"], context={"active_test": False})[0]
    chk("la plantilla vieja quedó archivada y renombrada '(versión anterior)'", not vieja["active"] and "versión anterior" in vieja["name"], vieja["name"])
    chk("el stock de las viejas quedó en 0 y el de las nuevas es el que había", rs["stock_viejas"] == 0 and rs["stock_nuevas"] == rs["stock_antes"] and rs["stock_por_medida_igual"], f"{rs['stock_antes']:.0f} → {rs['stock_nuevas']:.0f}")
    chk("el total de la bodega no cambió", abs(total_bodega() - total_antes) < 1e-6, f"{total_antes:.0f} → {total_bodega():.0f}")
    chk("0 asientos contables y 0 operaciones abiertas", call("account.move", "search_count", []) == asientos_antes and abiertas() == 0)
    nuevas = call("product.product", "search_read", [["product_tmpl_id", "=", tid_n]], fields=["id", "display_name", "lst_price", "default_code", "standard_price", "product_template_attribute_value_ids"])
    foto = {f["nombre"]: f for f in res["foto"]}
    chk("los 4 NOMBRES (con su código) son idénticos a los de antes ('[TR-27] Tapa pequeña (Alta, 13)')", {v["display_name"] for v in nuevas} == set(foto), str(sorted(v["display_name"] for v in nuevas)))
    chk("los 4 PRECIOS son idénticos a los de antes", all(abs(v["lst_price"] - foto[v["display_name"]]["precio"]) < 0.005 for v in nuevas))
    chk("los 4 CÓDIGOS están en las variantes nuevas (cada código lo tiene UN solo producto activo)", all(call("product.product", "search_count", [["default_code", "=", f["codigo"]]]) == 1 and call("product.product", "search", [["default_code", "=", f["codigo"]]])[0] in [v["id"] for v in nuevas] for f in res["foto"]))
    viejas_cod = {v["id"]: v["default_code"] for v in call("product.product", "read", [f["id"] for f in res["foto"]], fields=["default_code"], context={"active_test": False})}
    chk("las viejas conservan su código con '(anterior)' (no se perdió)", all(viejas_cod[f["id"]] == f"{f['codigo']} (anterior)" for f in res["foto"]), str(sorted(viejas_cod.values())))
    chk("precio base de la plantilla nueva en 0, y mismos ajustes que la vieja (categoría, tipo, impuestos)", call("product.template", "read", [tid_n], fields=["list_price"])[0]["list_price"] == 0 and call("product.template", "read", [tid_n], fields=CAMPOS_COMPARAR)[0] == {**call("product.template", "read", [tid], fields=CAMPOS_COMPARAR, context={"active_test": False})[0], "id": tid_n})
    chk("las líneas de venta históricas siguen apuntando a las viejas (historial intacto)", call("sale.order.line", "search_count", [["product_id", "in", [v["id"] for v in viejas]]]) >= lineas_ventas_antes)
    mapa_json = json.load(open(MAPA))
    chk("el mapa SUMÓ las 4 nuevas sin borrar lo que ya tenía", mapa_json.get("999999") == 888888 and all(str(v["id"]) in mapa_json for v in viejas) and len(mapa_json) == 5, f"{len(mapa_json)} entradas")
    chk("el mapa viejo→nuevo está cargado en el bot", bot.variante_vigente(A["id"]) != A["id"] and bot.variante_vigente(A["id"]) == res["mapa"][str(A["id"])])
    chk("la verificación de solo lectura da 4/4", res["verificacion"]["ok"] == 4 and not res["verificacion"]["problemas"], str(res["verificacion"]["problemas"][:2]))
    chk("Tapa mediana y Tapa grande (comparten el atributo «Libras de tapa») no se movieron: mismos precios y stock",
        {p["id"]: p["lst_price"] for p in call("product.product", "search_read", [["product_tmpl_id", "in", list(otras_plantillas)]], fields=["lst_price"])} == precios_otras
        and {p["id"]: p["qty_available"] for p in call("product.product", "search_read", [["product_tmpl_id", "in", list(otras_plantillas)]], fields=["qty_available"])} == stock_otras)
    nuevoA, nuevoB = bot.variante_vigente(A["id"]), bot.variante_vigente(B["id"])

    print("\n=== S. La búsqueda del bot sobre lo migrado")
    def busca(attrs, query="tapa pequeña", cat="tapas de radiador", n=20): return [p["display_name"] for p in bot.buscar_productos(query, atributos=attrs, categoria=cat, limit=n, relajar_categoria=False)]
    r = busca(["Alta", "16"]); chk("['Alta','16'] → exactamente '[TR-33] Tapa pequeña (Alta, 16)'", r == ["[TR-33] Tapa pequeña (Alta, 16)"], str(r))
    r = busca(["16", "Alta"]); chk("['16','Alta'] (al revés) → la misma", r == ["[TR-33] Tapa pequeña (Alta, 16)"], str(r))
    r = busca(["baja", "13"]); chk("['baja','13'] (minúscula) → '[TR-41] … (Baja, 13)'", r == ["[TR-41] Tapa pequeña (Baja, 13)"], str(r))
    r = busca(["Baja"]); chk("['Baja'] → las 2 bajas", sorted(r) == ["[TR-41] Tapa pequeña (Baja, 13)", "[TR-42] Tapa pequeña (Baja, 16)"], str(r))
    r = busca(["13"], query="tapa", n=30); chk("['13'] en tapas → las 2 pequeñas de 13 (y las medianas/grandes que ya tenían 13), ninguna de 16", {"[TR-27] Tapa pequeña (Alta, 13)", "[TR-41] Tapa pequeña (Baja, 13)"} <= set(r) and not any("(Alta, 16)" in x or "(Baja, 16)" in x for x in r), str(r))
    r = busca(None); chk("sin atributos → las 4 tapas pequeñas", len(r) == 4 and all("Tapa pequeña" in x for x in r), str(r))
    r = [p["display_name"] for p in bot.buscar_productos("TR-41", limit=5)]; chk("por código 'TR-41' → una sola (la nueva)", r == ["[TR-41] Tapa pequeña (Baja, 13)"], str(r))
    ids_viejas = {f["id"] for f in res["foto"]}
    r = [p["id"] for p in bot.buscar_productos("TR-27 (anterior)", limit=20)] + [p["id"] for p in bot.buscar_productos("TR-27", limit=20)] + [p["id"] for p in bot.buscar_productos("tapa pequeña", limit=20)]
    chk("ninguna búsqueda devuelve las variantes VIEJAS archivadas", not (set(r) & ids_viejas), str(sorted(set(r) & ids_viejas)))
    r = [p["display_name"] for p in bot.buscar_productos("corrugada", atributos=["2", "26"], limit=5)]; chk("corrugadas: ['2','26'] sigue sin traer tapas ni nada raro (en la copia puede ser la estructura vieja)", all("Tapa" not in x for x in r), str(r))

    print("\n=== V. Venta y precio individual sobre lo migrado")
    v = next(p for p in nuevas if p["display_name"] == "[TR-27] Tapa pequeña (Alta, 13)"); otros = {p["id"]: p["lst_price"] for p in nuevas if p["id"] != v["id"]}
    plan = bot.plan_precio(v["id"], 38000); chk("precio de UNA tapa pequeña ahora SÍ se puede (modo 'extra')", plan.get("modo") == "extra", str(plan.get("modo") or plan.get("error"))[:60])
    antes_v = v["lst_price"]; bot.actualizar_producto(v["id"], nuevo_precio=38000)
    desp = {p["id"]: p["lst_price"] for p in call("product.product", "search_read", [["product_tmpl_id", "=", tid_n]], fields=["lst_price"])}
    chk("la TR-27 cambió a $38,000 y las otras 3 quedaron IGUAL", desp[v["id"]] == 38000 and all(desp[i] == p for i, p in otros.items()), f"{antes_v:,.0f} → {desp[v['id']]:,.0f}")
    bot.actualizar_producto(v["id"], nuevo_precio=antes_v)
    bot.actualizar_producto(v["id"], nuevo_costo=12345); chk("el costo de UNA tapa se puede cambiar (y solo el de esa)", call("product.product", "read", [v["id"]], fields=["standard_price"])[0]["standard_price"] == 12345 and all(p["standard_price"] == 0 for p in call("product.product", "search_read", [["product_tmpl_id", "=", tid_n], ["id", "!=", v["id"]]], fields=["standard_price"])))
    bot.actualizar_producto(v["id"], nuevo_costo=0)
    bot.actualizar_producto(v["id"], nuevo_codigo="TR-27X"); chk("el código se puede cambiar en una sola variante (y queda único)", call("product.product", "search_count", [["default_code", "=", "TR-27X"]]) == 1)
    bot.actualizar_producto(v["id"], nuevo_codigo="TR-27")
    v2 = next(p for p in nuevas if p["display_name"] == "[TR-42] Tapa pequeña (Baja, 16)"); sv = stock_quant(v2["id"]); ov = bot.registrar_venta_completa(v2["id"], 1)
    chk("una venta nueva descuenta de la variante NUEVA", stock_quant(v2["id"]) == sv - 1)

    print("\n=== D. Devoluciones de ventas hechas ANTES de migrar")
    sn = stock_quant(nuevoA); so = stock_quant(A["id"])
    r = bot.aplicar_devolucion(oA, A["id"], 1)
    chk("venta A (entrega hecha, variante archivada): devolución aceptada", r["ok"] and not r["error_stock"], f"{r['msg']}" if r["ok"] else f"FALLÓ: {r['msg']}")
    chk("el stock vuelve a la variante NUEVA (+1) y la vieja queda en 0", stock_quant(nuevoA) == sn + 1 and stock_quant(A["id"]) == so, f"nueva {sn:.0f}→{stock_quant(nuevoA):.0f}, vieja {so:.0f}→{stock_quant(A['id']):.0f}")
    sn2 = stock_quant(nuevoB); so2 = stock_quant(B["id"])
    r = bot.aplicar_devolucion(oB, B["id"], 1)
    chk("venta B (entrega cancelada, camino manual): aceptada", r["ok"] and not r["error_stock"], r["msg"] if r["ok"] else f"FALLÓ: {r['msg']}")
    chk("y el stock vuelve a la variante NUEVA (+1), la vieja sigue en 0", stock_quant(nuevoB) == sn2 + 1 and stock_quant(B["id"]) == so2, f"nueva {sn2:.0f}→{stock_quant(nuevoB):.0f}")
    # por el chat: una venta ANTERIOR a la migración se encuentra por su código y muestra el stock de la variante vigente
    LOGMEM.clear(); registrar_en_log(oA, A["id"], A["display_name"], 1, sA); LOGMEM[-1]["devuelto"] = 0
    bot.obtener_pendiente = lambda u: None; guardado = {}; bot.guardar_pendiente = lambda u, a: guardado.update(a); bot.limpiar_pendiente = lambda u: None
    r = bot.handler_devolver("u", {"producto_query": codigo_A})
    chk(f"el chat encuentra la venta ANTERIOR por su código ({codigo_A}) y muestra el stock de la variante NUEVA", guardado.get("items") and guardado["items"][0]["stock"] == stock_quant(nuevoA), str([i.get("stock") for i in guardado.get("items", [])]))

    print("\n=== C. Con el MODELO real, sobre lo migrado")
    import copy as _copy
    mem = {}
    bot.obtener_pendiente = lambda u: _copy.deepcopy(mem.get(u)); bot.guardar_pendiente = lambda u, a: mem.__setitem__(u, _copy.deepcopy(a)); bot.limpiar_pendiente = lambda u: mem.pop(u, None)
    def dice(frase, n=3, hist=None):
        out = []
        for _ in range(n):
            mem.clear(); r, _h = bot.procesar_con_openai("u", frase, _copy.deepcopy(hist or [])); out.append((r, _copy.deepcopy(mem.get("u"))))
        return out
    o = dice("¿tienes tapa pequeña alta 16 libras?"); chk("'tapa pequeña alta 16 libras' → encuentra la TR-33 y solo esa", all("TR-33" in r and "TR-27" not in r and "TR-41" not in r for r, _ in o), f"{sum('TR-33' in r for r, _ in o)}/3")
    o = dice("hay tapa pequeña 13 baja"); chk("'tapa pequeña 13 baja' (orden al revés) → la TR-41", all("TR-41" in r and "TR-42" not in r for r, _ in o), f"{sum('TR-41' in r for r, _ in o)}/3")
    o = dice("se vendió una tapa pequeña alta 16"); chk("'se vendió una tapa pequeña alta 16' → vista previa de venta de la TR-33", all("TR-33" in r and "¿Confirmas" in r for r, _ in o), f"{sum('TR-33' in r and '¿Confirmas' in r for r, _ in o)}/3")
    o = dice("se vendieron 2 tapas pequeñas baja 13"); chk("'se vendieron 2 tapas pequeñas baja 13' → TR-41 ×2", all("TR-41" in r and "2 ×" in r for r, _ in o), f"{sum('TR-41' in r and '2 ×' in r for r, _ in o)}/3")
    o = dice("se vendió la TR-42"); chk("'se vendió la TR-42' → la TR-42", all("TR-42" in r and "¿Confirmas" in r for r, _ in o), f"{sum('TR-42' in r for r, _ in o)}/3")
    o = dice("el precio de la tapa pequeña alta 13 es 38000"); chk("precio de UNA tapa pequeña → vista previa solo de esa (las otras 3 no cambian)", all("TR-27" in r and "Precio:" in r and "las otras 3 no cambian" in r and "¿Confirmas" in r for r, _ in o), f"{sum('las otras 3' in r for r, _ in o)}/3")
    print("      " + o[0][0].replace("\n", "\n      "))
    o = dice("actualiza el costo de la TR-27 a 12000"); chk("costo de la TR-27 → solo esa", all("TR-27" in r and "Costo: $0 → $12,000" in r for r, _ in o), f"{sum('Costo: $0 → $12,000' in r for r, _ in o)}/3")
    o = dice("tapa de 13 libras"); chk("'tapa de 13 libras' (sin tipo) → trae las de 13 (alta y baja), ninguna de 16", all("TR-27" in r and "TR-41" in r and "TR-33" not in r and "TR-42" not in r for r, _ in o), f"{sum('TR-27' in r and 'TR-41' in r for r, _ in o)}/3")
    o = dice("se vendió la 5490"); chk("un código normal no se toca", all("[5490]" in r for r, _ in o))

    print("\n=== G. Invariantes finales")
    chk("0 asientos contables nuevos en toda la prueba", call("account.move", "search_count", []) == asientos_antes)
    chk("sin operaciones abiertas ni unidades reservadas", abiertas() == 0 and sum(q["reserved_quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["reserved_quantity"])) == 0)
except Exception:
    traceback.print_exc(); R.append(("EXCEPCIÓN en la prueba", False))
malos = [t for t, c in R if not c]
print(f"\n{len(R) - len(malos)}/{len(R)} comprobaciones OK" + ("" if not malos else "  ← FALLARON: " + "; ".join(malos)))
sys.exit(1 if malos else 0)
