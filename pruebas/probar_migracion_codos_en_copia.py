# Prueba COMPLETA de la migración de los CODOS (azul, negro, rojo) contra la COPIA temporal (puerto 8070): migra, verifica y prueba el bot sobre lo migrado.
# Primero:  ./copia_odoo.sh arrancar   Después:  python3 probar_migracion_codos_en_copia.py   Al terminar:  ./copia_odoo.sh parar
import os, sys, json, re, time, traceback, copy as _copy
from pathlib import Path
os.environ["ODOO_URL"] = "http://127.0.0.1:8070"; os.environ["ODOO_DB"] = "radibombas_prueba"; os.environ["MODO_ENTREGA"] = "odoo"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "pruebas"))
import logging; logging.disable(logging.CRITICAL)
import bot, migrar_codos
assert bot.ODOO_DB == "radibombas_prueba" and bot.ODOO_URL.endswith(":8070"), "¡NO está apuntando a la copia!"
LOGMEM = []
CAMPOS_COMPARAR = ["categ_id", "type", "is_storable", "tracking", "uom_id", "taxes_id", "supplier_taxes_id", "sale_ok", "purchase_ok", "invoice_policy"]
bot.cargar_ventas_log = lambda: LOGMEM; bot.guardar_ventas_log = lambda d: None
FOTO, MAPA = "/tmp/migracion_codos_test.json", "/tmp/mapa_variantes_codos_test.json"
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
    # ── elegir variantes con stock para las ventas de «antes»
    def activa(tid, minimo=2):
        vs = call("product.product", "search_read", [["product_tmpl_id", "=", tid], ["qty_available", ">=", minimo]], fields=["id", "display_name", "qty_available", "default_code"], order="id")
        return vs[0]
    A = activa(357); B = activa(358); C = activa(118)       # A: codo negro 12" (modo odoo) · B: codo rojo 12" (modo simple) · C: codo azul 2 1/2 (modo odoo)
    print(f"Copia lista. A={A['display_name']} ({A['qty_available']:.0f}) · B={B['display_name']} ({B['qty_available']:.0f}) · C={C['display_name']} ({C['qty_available']:.0f})")
    ids_origen = [t for f in migrar_codos.FAMILIAS for t in f["origen"]]
    ventas_antes = call("sale.order.line", "search_count", [["product_id.product_tmpl_id", "in", ids_origen]], context={"active_test": False})
    otras_ids = [110, 114, 108, 109, 106, 472, 113, 115, 111, 107, 367, 475, 368, 370, 85, 371, 372, 81]
    def foto_otras(): return {p["id"]: (p["lst_price"], p["qty_available"], p["default_code"]) for p in call("product.product", "search_read", [["product_tmpl_id", "in", otras_ids]], fields=["lst_price", "qty_available", "default_code"])}
    otras_antes = foto_otras()

    print("\n=== P. ANTES de migrar: tres ventas con las variantes viejas")
    sA = stock_quant(A["id"]); oA = bot.registrar_venta_completa(A["id"], 1); registrar_en_log(oA, A["id"], A["display_name"], 1, sA)
    bot.MODO_ENTREGA = "simple"; sB = stock_quant(B["id"]); oB = bot.registrar_venta_completa(B["id"], 1); registrar_en_log(oB, B["id"], B["display_name"], 1, sB); bot.MODO_ENTREGA = "odoo"
    sC = stock_quant(C["id"]); oC = bot.registrar_venta_completa(C["id"], 1); registrar_en_log(oC, C["id"], C["display_name"], 1, sC)
    chk("ventas A (negro, modo odoo), B (rojo, modo simple) y C (azul, modo odoo) hechas", stock_quant(A["id"]) == sA - 1 and stock_quant(B["id"]) == sB - 1 and stock_quant(C["id"]) == sC - 1)
    total_antes = total_bodega(); asientos_antes = call("account.move", "search_count", [])

    print("\n=== M. La migración")
    t0 = time.time(); res = migrar_codos.migrar(bot, FOTO, MAPA); dt = time.time() - t0
    rs = res["resumen"]; print(f"      (tardó {dt:.0f} s)")
    pn = res["plantillas_nuevas"]
    chk("63 variantes nuevas: azul 3, negro 36, rojo 24, en TRES plantillas nuevas", rs["variantes_nuevas"] == 63 and set(pn) == {"Codo azul", "Codo negro", "Codo rojo"}
        and [call("product.product", "search_count", [["product_tmpl_id", "=", pn[n]]]) for n in ("Codo azul", "Codo negro", "Codo rojo")] == [3, 36, 24])
    chk("las 63 variantes VIEJAS siguen existiendo, TODAS archivadas (ninguna se borró)", rs["viejas_archivadas"] == 63 and rs["viejas_eliminadas"] == 0, f"archivadas={rs['viejas_archivadas']}, eliminadas={rs['viejas_eliminadas']}")
    viejas_t = call("product.template", "read", ids_origen, fields=["name", "active"], context={"active_test": False})
    chk("las 8 plantillas viejas quedaron archivadas y renombradas '(versión anterior)'", len(viejas_t) == 8 and all(not t["active"] and "(versión anterior)" in t["name"] for t in viejas_t), str([t["name"] for t in viejas_t][:3]))
    chk("el stock de las viejas quedó en 0 y el de las nuevas es el que había", rs["stock_viejas"] == 0 and rs["stock_nuevas"] == rs["stock_antes"] and rs["stock_por_variante_igual"], f"{rs['stock_antes']:.0f} → {rs['stock_nuevas']:.0f}")
    chk("el total de la bodega no cambió", abs(total_bodega() - total_antes) < 1e-6, f"{total_antes:.0f} → {total_bodega():.0f}")
    chk("0 asientos contables y 0 operaciones abiertas", call("account.move", "search_count", []) == asientos_antes and abiertas() == 0)
    foto = {(f["familia"], f["medida"]): f for f in res["foto"]}
    nuevas = {}
    for n, tid in pn.items():
        for v in call("product.product", "search_read", [["product_tmpl_id", "=", tid]], fields=["id", "display_name", "lst_price", "standard_price", "default_code", "product_template_attribute_value_ids"]):
            nuevas[v["display_name"]] = v
    esperados = {(f"[{f['codigo']}] " if f["codigo"] else "") + f"{f['familia']} ({f['medida']})" for f in res["foto"]}
    chk("los 63 NOMBRES son «[código] Codo negro (2 1/2, 20)» (diámetro, largo)", set(nuevas) == esperados, str(sorted(nuevas)[:2]))
    chk("los 63 PRECIOS son idénticos a los de antes", all(abs(nuevas[(f"[{f['codigo']}] " if f["codigo"] else "") + f"{f['familia']} ({f['medida']})"]["lst_price"] - f["precio"]) < 0.005 for f in res["foto"]))
    chk("los COSTOS se trasladaron (3 en codo negro)", sorted(v["standard_price"] for v in nuevas.values() if v["standard_price"]) == sorted(f["costo"] for f in res["foto"] if f["costo"]) and sum(1 for f in res["foto"] if f["costo"]) == 3)
    cods = [f["codigo"] for f in res["foto"] if f["codigo"]]
    chk("cada CÓDIGO lo tiene UN solo producto activo (la variante nueva)", all(call("product.product", "search_count", [["default_code", "=", c]]) == 1 for c in cods) and
        {call("product.product", "search", [["default_code", "=", c]])[0] for c in cods} <= {v["id"] for v in nuevas.values()}, f"{len(cods)} códigos")
    vcod = {v["id"]: v["default_code"] for v in call("product.product", "read", [f["id"] for f in res["foto"]], fields=["default_code"], context={"active_test": False})}
    chk("las viejas conservan su código con '(anterior)'", all(vcod[f["id"]] == f"{f['codigo']} (anterior)" for f in res["foto"] if f["codigo"]))
    chk("precio base 0 en las tres plantillas nuevas y mismos ajustes que las viejas (categoría, tipo, impuestos)", all(call("product.template", "read", [tid], fields=["list_price"])[0]["list_price"] == 0 for tid in pn.values())
        and call("product.template", "read", [pn["Codo negro"]], fields=CAMPOS_COMPARAR)[0] == {**call("product.template", "read", [357], fields=CAMPOS_COMPARAR, context={"active_test": False})[0], "id": pn["Codo negro"]})
    chk("las líneas de venta históricas siguen apuntando a las viejas (historial intacto)", call("sale.order.line", "search_count", [["product_id.product_tmpl_id", "in", ids_origen]], context={"active_test": False}) >= ventas_antes)
    mapa_json = json.load(open(MAPA))
    chk("el mapa SUMÓ las 63 sin borrar lo que ya tenía", mapa_json.get("999999") == 888888 and len(mapa_json) == 64, f"{len(mapa_json)} entradas")
    chk("la verificación de solo lectura da 63/63", res["verificacion"]["ok"] == 63 and not res["verificacion"]["problemas"], str(res["verificacion"]["problemas"][:2]))
    chk("semicodos, reductores y Ele NO se movieron (mismos precios, códigos y stock)", foto_otras() == otras_antes)
    nuevoA, nuevoB, nuevoC = bot.variante_vigente(A["id"]), bot.variante_vigente(B["id"]), bot.variante_vigente(C["id"])
    chk("el mapa viejo→nuevo está cargado en el bot", all(n != o for o, n in ((A["id"], nuevoA), (B["id"], nuevoB), (C["id"], nuevoC))))

    def fila(fam, medida): f = foto[(fam, medida)]; return f
    def nombre(fam, medida): f = fila(fam, medida); return (f"[{f['codigo']}] " if f["codigo"] else "") + f"{fam} ({medida})"

    print("\n=== S. La búsqueda del bot sobre lo migrado")
    def busca(query, attrs, cat="codos", n=40): return [p["display_name"] for p in bot.buscar_productos(query, atributos=attrs, categoria=cat, limit=n, relajar_categoria=False)]
    r = busca("negro", ["2 1/2", "20"]); chk("negro ['2 1/2','20'] → exactamente esa", r == [nombre("Codo negro", "2 1/2, 20")], str(r))
    r = busca("negro", ["20", "2 1/2"]); chk("negro ['20','2 1/2'] (al revés) → la misma", r == [nombre("Codo negro", "2 1/2, 20")], str(r))
    r = busca("rojo", ["2", "16"]); chk("rojo ['2','16'] → exactamente esa", r == [nombre("Codo rojo", "2, 16")], str(r))
    r = busca("rojo", ["2", "20"]); chk("rojo ['2','20'] → ninguna (el rojo no tiene 20)", all("Codo rojo (2, 20)" not in x for x in r), str(r)[:120])
    r = busca("azul", ["1 3/4"]); chk("azul ['1 3/4'] → exactamente el azul de 1 3/4", r == ["Codo azul (1 3/4)"], str(r))
    r = busca("negro", ["2 1/2"]); chk("negro ['2 1/2'] (solo diámetro) → las 3 medidas (12, 20, 28) del codo negro (+ lo que traiga la búsqueda por nombre)", {nombre("Codo negro", f"2 1/2, {l}") for l in (12, 20, 28)} <= set(r), str(r)[:200])
    r = [p["display_name"] for p in bot.buscar_productos("82081", limit=5)]; chk("por código '82081' → una sola (la nueva)", r == [nombre("Codo negro", "2 1/2, 20")], str(r))
    r = busca("semicodo", ["2 1/2"]); chk("semicodos siguen como antes (diámetro suelto): aparecen y no son «Codo negro»", r and all("Semicodo" in x for x in r), str(r)[:160])
    r = [p["display_name"] for p in bot.buscar_productos("reductor rojo 3 a 2 1/2", categoria="codos", limit=5, relajar_categoria=False)]; chk("reductores siguen como antes", any("reductor" in x.lower() for x in r), str(r)[:120])
    ids_viejas = {f["id"] for f in res["foto"]}
    r = [p["id"] for p in bot.buscar_productos("codo negro", categoria="codos", limit=60)] + [p["id"] for p in bot.buscar_productos("82081", limit=20)] + [p["id"] for p in bot.buscar_productos("codo rojo", categoria="codos", limit=60)]
    chk("ninguna búsqueda devuelve las variantes VIEJAS archivadas", not (set(r) & ids_viejas))

    print("\n=== T. Las tablas de «falta un dato»")
    t = bot.pregunta_medida_faltante("negro", ["2 1/2"], "codos", para="vender", usuario="u")
    chk("negro de 2 1/2 → tabla de los 3 largos con precio y stock + pregunta", t and all(f"• {l}:" in t for l in ("12", "20", "28")) and "¿Qué largo necesitas?" in t and "Semicodo" not in t, (t or "None").replace("\n", " | ")[:200])
    t = bot.pregunta_medida_faltante("negro", ["20"], "codos", para="buscar", usuario="u")
    chk("negro de 20 → tabla de los 12 diámetros, sin pregunta (consulta)", t and "• 1:" in t and "• 4:" in t and t.count("•") == 12 and "¿Qué" not in t, f"{(t or '').count('•')} filas")
    t = bot.pregunta_medida_faltante("rojo", ["2"], "codos", para="vender", usuario="u")
    chk("rojo de 2 → tabla de los 2 largos (12 y 16)", t and "• 12:" in t and "• 16:" in t and t.count("•") == 2, (t or "None")[:100])
    t = bot.pregunta_medida_faltante("codo negro", None, "codos", para="vender", usuario="u")
    chk("«se vendió codo negro» sin medida → explica diámetro y largo (12, 20, 28)", t and "se define por diámetro y largo" in t and "Largos: 12, 20, 28" in t and "Diámetros: 1, 1 1/4" in t, (t or "None").replace("\n", " | ")[:200])
    chk("azul de 1 3/4 (una sola dimensión) → sin tabla", bot.pregunta_medida_faltante("azul", ["1 3/4"], "codos", para="buscar") is None)
    chk("semicodo de 2 1/2 → sin tabla", bot.pregunta_medida_faltante("semicodo", ["2 1/2"], "codos", para="buscar") is None)
    chk("codo sin color y sin medida → sin tabla (negro y rojo mezclados)", bot.pregunta_medida_faltante("codo", None, "codos", para="vender") is None)

    print("\n=== V. Venta y precio individual sobre lo migrado")
    v = nuevas[nombre("Codo negro", "2 1/2, 20")]; otros = {p["id"]: p["lst_price"] for p in nuevas.values() if p["id"] != v["id"]}
    plan = bot.plan_precio(v["id"], 120000); chk("precio de UN codo negro ahora SÍ se puede (modo 'extra')", plan.get("modo") == "extra", str(plan.get("modo") or plan.get("error"))[:60])
    antes_v = v["lst_price"]; bot.actualizar_producto(v["id"], nuevo_precio=120000)
    desp = {p["id"]: p["lst_price"] for p in call("product.product", "search_read", [["product_tmpl_id", "in", list(pn.values())]], fields=["lst_price"])}
    chk("el (2 1/2, 20) cambió a $120,000 y las otras 62 quedaron IGUAL", desp[v["id"]] == 120000 and all(desp[i] == p for i, p in otros.items()), f"{antes_v:,.0f} → {desp[v['id']]:,.0f}")
    bot.actualizar_producto(v["id"], nuevo_precio=antes_v)
    bot.actualizar_producto(v["id"], nuevo_costo=12345); chk("el costo de UN codo se puede cambiar (y solo el de ese)", call("product.product", "read", [v["id"]], fields=["standard_price"])[0]["standard_price"] == 12345)
    bot.actualizar_producto(v["id"], nuevo_costo=v["standard_price"])
    v2 = nuevas[nombre("Codo rojo", "2, 16")]; sv = stock_quant(v2["id"]); ov = bot.registrar_venta_completa(v2["id"], 1)
    chk("una venta nueva descuenta de la variante NUEVA", stock_quant(v2["id"]) == sv - 1)

    print("\n=== D. Devoluciones de ventas hechas ANTES de migrar")
    for etiqueta, o, viejo, nuevo in (("A negro (entrega hecha)", oA, A, nuevoA), ("B rojo (entrega cancelada, camino manual)", oB, B, nuevoB), ("C azul (entrega hecha)", oC, C, nuevoC)):
        sn = stock_quant(nuevo); so = stock_quant(viejo["id"])
        r = bot.aplicar_devolucion(o, viejo["id"], 1)
        chk(f"venta {etiqueta}: devolución aceptada y el stock vuelve a la variante NUEVA (+1), la vieja sigue en 0",
            r["ok"] and not r["error_stock"] and stock_quant(nuevo) == sn + 1 and stock_quant(viejo["id"]) == so, (r["msg"] if r["ok"] else f"FALLÓ: {r['msg']}")[:80])
    LOGMEM.clear(); registrar_en_log(oA, A["id"], A["display_name"], 1, sA)
    bot.obtener_pendiente = lambda u: None; guardado = {}; bot.guardar_pendiente = lambda u, a: guardado.update(a); bot.limpiar_pendiente = lambda u: None
    r = bot.handler_devolver("u", {"producto_query": A["default_code"]})
    chk(f"el chat encuentra la venta ANTERIOR por su código ({A['default_code']}) y muestra el stock de la variante NUEVA", guardado.get("items") and guardado["items"][0]["stock"] == stock_quant(nuevoA), str([i.get("stock") for i in guardado.get("items", [])]))

    print("\n=== C. Con el MODELO real, sobre lo migrado")
    mem = {}
    bot.obtener_pendiente = lambda u: _copy.deepcopy(mem.get(u)); bot.guardar_pendiente = lambda u, a: mem.__setitem__(u, _copy.deepcopy(a)); bot.limpiar_pendiente = lambda u: mem.pop(u, None)
    def conv(mensajes, n=3):
        out = []
        for _ in range(n):
            mem.clear(); bot.MEDIDA_PREGUNTADA.clear(); hist = []; paso = []
            for m in mensajes:
                r, hist = bot.procesar_con_openai("u", m, hist); paso.append((r, _copy.deepcopy(mem.get("u"))))
            out.append(paso)
        return out
    def prueba(titulo, mensajes, cond, n=3):
        o = conv(mensajes, n); ok = sum(1 for p in o if cond(p)); chk(titulo, ok == n, f"{ok}/{n}")
        return o
    nm = lambda fam, med: nombre(fam, med)
    prueba("'¿tienes codo negro de 2 1/2 por 20?' → solo el (2 1/2, 20)", ["¿tienes codo negro de 2 1/2 por 20?"], lambda p: nm("Codo negro", "2 1/2, 20") in p[0][0] and "(2 1/2, 12)" not in p[0][0] and "Hay más" not in p[0][0])
    prueba("'hay codo rojo dos y media por doce' → el rojo (2 1/2, 12)", ["hay codo rojo dos y media por doce"], lambda p: nm("Codo rojo", "2 1/2, 12") in p[0][0] and "Codo negro" not in p[0][0])
    prueba("'se vendió un codo negro de 3 por 28' → vista previa de esa venta", ["se vendió un codo negro de 3 por 28"], lambda p: nm("Codo negro", "3, 28") in p[0][0] and "¿Confirmas" in p[0][0] and (p[0][1] or {}).get("tipo") == "venta")
    o = prueba("'se vendieron 2 codos rojos de 2 por 16' → rojo (2, 16) ×2", ["se vendieron 2 codos rojos de 2 por 16"], lambda p: nm("Codo rojo", "2, 16") in p[0][0] and "2 ×" in p[0][0] and "¿Confirmas" in p[0][0])
    prueba("'se vendió un codo azul de 1 3/4' → vista previa del azul", ["se vendió un codo azul de 1 3/4"], lambda p: "Codo azul (1 3/4)" in p[0][0] and "¿Confirmas" in p[0][0])
    prueba("'se vendió un codo azul de uno y tres cuartos' (en palabras) → el mismo", ["se vendió un codo azul de uno y tres cuartos"], lambda p: "Codo azul (1 3/4)" in p[0][0] and "¿Confirmas" in p[0][0])
    prueba("'se vendió codo negro de 3' → tabla de largos + pregunta; 'la de 20' → venta (3, 20)", ["se vendió codo negro de 3", "la de 20"],
           lambda p: all(f"• {l}:" in p[0][0] for l in ("12", "20", "28")) and "¿Qué largo necesitas?" in p[0][0] and nm("Codo negro", "3, 20") in p[1][0] and "¿Confirmas" in p[1][0])
    prueba("'codo negro de 20' → tabla de diámetros; 'la de dos y media' (venta) → (2 1/2, 20)", ["se vendió codo negro de 20", "la de dos y media"],
           lambda p: "• 4:" in p[0][0] and nm("Codo negro", "2 1/2, 20") in p[1][0] and "¿Confirmas" in p[1][0])
    prueba("'se vendió codo rojo de 2' → tabla (12 y 16); 'la de 16' → rojo (2, 16)", ["se vendió codo rojo de 2", "la de 16"],
           lambda p: "• 12:" in p[0][0] and "• 16:" in p[0][0] and nm("Codo rojo", "2, 16") in p[1][0] and "¿Confirmas" in p[1][0])
    prueba("'se vendió codo negro' (sin medida) → explica diámetro y largo", ["se vendió codo negro"], lambda p: "se define por diámetro y largo" in p[0][0] and "12, 20, 28" in p[0][0])
    prueba("'el precio del codo negro de 2 1/2 por 20 es 120000' → solo esa (las otras 35 no cambian)", ["el precio del codo negro de 2 1/2 por 20 es 120000"],
           lambda p: nm("Codo negro", "2 1/2, 20") in p[0][0] and "$120,000" in p[0][0] and "las otras 35 no cambian" in p[0][0] and "¿Confirmas" in p[0][0])
    prueba("'actualiza el costo del codo rojo de 2 por 12 a 15000' → solo ese", ["actualiza el costo del codo rojo de 2 por 12 a 15000"], lambda p: nm("Codo rojo", "2, 12") in p[0][0] and "Costo:" in p[0][0] and "$15,000" in p[0][0])
    prueba("'se vendió la 82081' (por código) → el (2 1/2, 20)", ["se vendió la 82081"], lambda p: nm("Codo negro", "2 1/2, 20") in p[0][0] and "¿Confirmas" in p[0][0])
    prueba("'semicodo negro 2 1/2' sigue como antes (semicodos, sin tabla de largos)", ["semicodo negro 2 1/2"], lambda p: "Semicodo" in p[0][0] and "Codo negro (" not in p[0][0] and "¿Qué largo" not in p[0][0])
    prueba("'codo reductor rojo 3 a 2 1/2' → lista de reductores con el que pide (antes salía una lista cualquiera)", ["codo reductor rojo 3 a 2 1/2"], lambda p: "Codo reductor rojo 3 a 2 1/2" in p[0][0] and "Codo rojo (" not in p[0][0] and "Codo negro (" not in p[0][0])
    prueba("corrugadas siguen: 'se vendió corrugada de 2 por 14' → (2, 14)", ["se vendió corrugada de 2 por 14"], lambda p: "(2, 14)" in p[0][0] and "¿Confirmas" in p[0][0])
    prueba("corrugadas siguen: 'se vendió corrugada de dos' → tabla + 'la de 14' → (2, 14)", ["se vendió corrugada de dos", "la de 14"], lambda p: "¿Qué largo" in p[0][0] and "(2, 14)" in p[1][0])
    prueba("tapas siguen: 'tapa pequeña baja 13' → la TR-41", ["tapa pequeña baja 13"], lambda p: "TR-41" in p[0][0] and "TR-42" not in p[0][0])
    prueba("un código normal no se toca: 'se vendió la 5490'", ["se vendió la 5490"], lambda p: "[5490]" in p[0][0])

    print("\n=== G. Invariantes finales")
    chk("0 asientos contables nuevos en toda la prueba", call("account.move", "search_count", []) == asientos_antes)
    chk("sin operaciones abiertas ni unidades reservadas", abiertas() == 0 and sum(q["reserved_quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["reserved_quantity"])) == 0)
except Exception:
    traceback.print_exc(); R.append(("EXCEPCIÓN en la prueba", False))
malos = [t for t, c in R if not c]
print(f"\n{len(R) - len(malos)}/{len(R)} comprobaciones OK" + ("" if not malos else "  ← FALLARON: " + "; ".join(malos)))
sys.exit(1 if malos else 0)
