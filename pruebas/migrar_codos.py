# Migración de los CODOS a tres productos: «Codo azul», «Codo negro» y «Codo rojo» (misma receta que las corrugadas y la tapa pequeña).
#
#   Hoy:   Codo azul 1 1/2 / 1 3/4 / 2 1/2           = 3 productos sueltos (sin variantes)
#          Codo negro (12") / (20") / (28")          = 3 plantillas, cada una con 12 diámetros
#          Codo rojo (12") / (16")                   = 2 plantillas, cada una con 12 diámetros
#   Queda: Codo azul  = 1 producto, atributo «diámetro codo azul» (3 valores: «1 1/2», «1 3/4», «2 1/2»)
#          Codo negro = 1 producto, atributo «medida codo negro» (36 valores «diámetro, largo»: «2 1/2, 20»)
#          Codo rojo  = 1 producto, atributo «medida codo rojo»  (24 valores «diámetro, largo»: «2 1/2, 16»)
#   Precio base de cada producto nuevo = 0 y el «extra» de cada valor = el precio que ya tenía → todos los precios quedan IGUALES y cada variante
#   puede cambiarse sola. Los CÓDIGOS (82009, 882043-B…) y los COSTOS pasan a la variante nueva; la vieja queda con «CÓDIGO (anterior)».
#
#   python3 migrar_codos.py --destino copia [--dry-run]                  (contra la copia temporal de pruebas, puerto 8070)
#   python3 migrar_codos.py --destino produccion --si-estoy-seguro       (con el bot detenido y un respaldo de menos de 30 minutos)
#   python3 migrar_codos.py --destino produccion --verificar <foto.json> (solo lectura, cuando quieras)
#
# Las plantillas viejas se ARCHIVAN completas, renombradas «… (versión anterior)» (con sus variantes e historial; no se borra nada → reversible).
# Orden (cualquier cosa rara aborta ANTES de escribir): 0 comprobaciones de todas las familias · 1 foto JSON · 2 atributo y plantilla nuevos ·
# 3 precios · 4 códigos y costos · 5 stock (ajustes de inventario con historial) · 6 archivar las viejas · 7 SUMAR al mapa viejo→nuevo
# (data/mapa_variantes.json; no borra lo que ya tienen las corrugadas y la tapa) y verificar fila por fila.
import os, sys, json, re, time, argparse
from collections import Counter

CATEGORIA_ID = 87   # Mangueras / Codos
FAMILIAS = [
    {"nombre": "Codo azul", "atributo": "diámetro codo azul", "origen": [117, 116, 118], "tipo": "simple"},
    {"nombre": "Codo negro", "atributo": "medida codo negro", "origen": [357, 473, 474], "tipo": "medida"},
    {"nombre": "Codo rojo", "atributo": "medida codo rojo", "origen": [358, 448], "tipo": "medida"},
]
CAMPOS_COPIAR = ["categ_id", "type", "is_storable", "tracking", "uom_id", "taxes_id", "supplier_taxes_id", "sale_ok", "purchase_ok",
                 "description", "description_sale", "description_purchase", "invoice_policy", "service_type", "sequence", "image_1920"]


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _num(s):
    p = s.split(); total = 0.0
    for t in p:
        if "/" in t:
            a, b = t.split("/"); total += float(a) / float(b)
        else:
            total += float(t)
    return total


def migrar(bot, salida_foto, salida_mapa, dry_run=False, salida_csv=None):
    uid, models = bot.odoo_connect()
    def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)

    # ── 0. Comprobaciones (todas las familias, antes de escribir NADA) ──────────────────
    filas = []          # una por variante vieja ACTIVA
    for fam in FAMILIAS:
        for tid in fam["origen"]:
            t = call("product.template", "read", [tid], fields=["name", "active", "categ_id", "attribute_line_ids", "sale_ok"])[0]
            assert t["active"] and t["sale_ok"] and t["categ_id"][0] == CATEGORIA_ID, f"Plantilla {tid} ({t['name']}) no está activa/vendible en la categoría Codos"
            vs = call("product.product", "search_read", [["product_tmpl_id", "=", tid]], fields=["id", "display_name", "lst_price", "standard_price", "default_code", "barcode", "product_template_attribute_value_ids"])
            assert vs and not any(v["barcode"] for v in vs), f"{t['name']}: sin variantes activas o con códigos de barras (el script no los traslada)"
            if fam["tipo"] == "simple":
                m = re.fullmatch(rf"{fam['nombre']} (\d(?: \d/\d)?)", t["name"])
                assert m and not t["attribute_line_ids"] and len(vs) == 1, f"{t['name']}: se esperaba un producto simple «{fam['nombre']} <diámetro>»"
                filas.append({"familia": fam["nombre"], "plantilla": tid, "id": vs[0]["id"], "diametro": m.group(1), "largo": None, "medida": m.group(1), "nombre": vs[0]["display_name"],
                              "codigo": vs[0]["default_code"] or False, "precio": vs[0]["lst_price"], "costo": vs[0]["standard_price"]})
            else:
                m = re.fullmatch(rf"{fam['nombre']} \((\d+)\"\)", t["name"])
                assert m, f"{t['name']}: se esperaba «{fam['nombre']} (<largo>\")»"
                largo = m.group(1)
                lineas = call("product.template.attribute.line", "read", t["attribute_line_ids"], fields=["attribute_id", "value_ids"])
                assert len(lineas) == 1 and len(lineas[0]["value_ids"]) == len(vs) == 12, f"{t['name']}: se esperaba 1 atributo con 12 valores y 12 variantes activas (hay {len(lineas)} / {len(vs)})"
                ptav = {p["id"]: p["name"] for p in call("product.template.attribute.value", "search_read", [["product_tmpl_id", "=", tid]], fields=["name"])}
                for v in vs:
                    assert len(v["product_template_attribute_value_ids"]) == 1, f"{v['display_name']}: debe tener UN valor de atributo"
                    d = ptav[v["product_template_attribute_value_ids"][0]]
                    filas.append({"familia": fam["nombre"], "plantilla": tid, "id": v["id"], "diametro": d, "largo": largo, "medida": f"{d}, {largo}", "nombre": v["display_name"],
                                  "codigo": v["default_code"] or False, "precio": v["lst_price"], "costo": v["standard_price"]})
        assert not call("product.attribute", "search_count", [["name", "=", fam["atributo"]]]), f"Ya existe el atributo «{fam['atributo']}» (¿ya se migró?)"
        medidas = [f["medida"] for f in filas if f["familia"] == fam["nombre"]]
        assert len(medidas) == len(set(medidas)), f"{fam['nombre']}: medidas repetidas"
    ids_v = [f["id"] for f in filas]
    codigos = [f["codigo"] for f in filas if f["codigo"]]
    assert len(codigos) == len(set(codigos)), "Códigos repetidos entre las variantes"
    otros = call("product.product", "search_count", [["default_code", "in", codigos], ["id", "not in", ids_v]]) if codigos else 0
    assert otros == 0, f"Algún código de estos codos lo usa OTRO producto activo ({otros})"
    assert call("sale.order.line", "search_count", [["product_id", "in", ids_v], ["state", "in", ["draft", "sent"]]]) == 0, "Hay órdenes en borrador con estos codos"
    assert call("stock.move", "search_count", [["product_id", "in", ids_v], ["state", "not in", ["done", "cancel"]]]) == 0, "Hay movimientos pendientes"
    quants = call("stock.quant", "search_read", [["product_id", "in", ids_v], ["location_id.usage", "=", "internal"]], fields=["product_id", "quantity", "reserved_quantity"])
    assert not any(q["reserved_quantity"] for q in quants), "Hay unidades reservadas"
    assert not any(q["quantity"] < 0 for q in quants), "Hay stock negativo"
    # variantes ARCHIVADAS de estas plantillas con stock (fantasmas): deben estar ya en 0 o el total no cuadraría
    fantasmas = call("stock.quant", "search_read", [["product_id.product_tmpl_id", "in", [t for f in FAMILIAS for t in f["origen"]]], ["product_id", "not in", ids_v], ["location_id.usage", "=", "internal"], ["quantity", "!=", 0]], fields=["quantity"], context={"active_test": False})
    assert not fantasmas, f"Hay {len(fantasmas)} variantes ARCHIVADAS con stock en estas plantillas: ponlas en 0 antes"
    stock = {}
    for q in quants: stock[q["product_id"][0]] = stock.get(q["product_id"][0], 0) + q["quantity"]
    for f in filas: f["stock"] = stock.get(f["id"], 0)
    log("0. Comprobaciones OK:", len(filas), "variantes en", len(FAMILIAS), "familias,", sum(1 for f in filas if f["stock"]), "con stock,", f"{sum(f['stock'] for f in filas):.0f} unidades")
    for fam in FAMILIAS:
        fs = [f for f in filas if f["familia"] == fam["nombre"]]
        log(f"   {fam['nombre']}: {len(fs)} variantes, {sum(f['stock'] for f in fs):.0f} unidades, {sum(1 for f in fs if f['codigo'])} con código, {sum(1 for f in fs if f['costo'])} con costo")

    # ── 1. Foto ─────────────────────────────────────────────────────────────────────
    json.dump({"variantes": filas}, open(salida_foto, "w"), ensure_ascii=False, indent=1)
    log("1. Foto guardada en", salida_foto)
    if dry_run:
        log("DRY-RUN: no se escribió nada en Odoo."); return {"foto": filas}

    contadores_antes = {"asientos": call("account.move", "search_count", []), "stock_total": sum(q["quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["quantity"]))}
    campos = call("product.template", "fields_get", attributes=["type"])
    quiero = [c for c in CAMPOS_COPIAR if c in campos]
    nueva_de = {}        # id variante vieja → id variante nueva
    plantillas_nuevas = {}
    for fam in FAMILIAS:
        fs = [f for f in filas if f["familia"] == fam["nombre"]]
        # ── 2. Atributo y plantilla nuevos ──
        orden = sorted(fs, key=lambda f: ((_num(f["largo"]) if f["largo"] else 0), _num(f["diametro"])))
        attr_id = call("product.attribute", "create", {"name": fam["atributo"], "display_type": "select", "create_variant": "always"})
        val_ids = call("product.attribute.value", "create", [{"name": f["medida"], "attribute_id": attr_id, "sequence": i + 1} for i, f in enumerate(orden)])
        base = call("product.template", "read", [fam["origen"][0]], fields=quiero)[0]
        for tid in fam["origen"][1:]:
            otra = call("product.template", "read", [tid], fields=[c for c in quiero if c not in ("image_1920", "sequence")])[0]
            assert all(otra[c] == base[c] for c in otra if c != "id"), f"{fam['nombre']}: las plantillas de origen no tienen los mismos ajustes ({tid})"
        ajustes = {}
        for c in quiero:
            v = base[c]
            if campos[c]["type"] == "many2one":
                if v: ajustes[c] = v[0]
            elif campos[c]["type"] in ("many2many", "one2many"):
                ajustes[c] = [(6, 0, v)]
            elif v is not False or campos[c]["type"] == "boolean":
                ajustes[c] = v
        ajustes.update({"name": fam["nombre"], "list_price": 0, "active": True, "attribute_line_ids": [(0, 0, {"attribute_id": attr_id, "value_ids": [(6, 0, val_ids)]})]})
        tid_nuevo = call("product.template", "create", ajustes)
        plantillas_nuevas[fam["nombre"]] = tid_nuevo
        nuevas = call("product.product", "search_read", [["product_tmpl_id", "=", tid_nuevo]], fields=["id", "display_name", "product_template_attribute_value_ids"])
        assert len(nuevas) == len(fs), f"{fam['nombre']}: se esperaban {len(fs)} variantes nuevas activas; hay {len(nuevas)}"
        ptav_n = {p["id"]: p for p in call("product.template.attribute.value", "search_read", [["product_tmpl_id", "=", tid_nuevo], ["attribute_id", "=", attr_id]], fields=["name", "price_extra"])}
        por_medida = {ptav_n[v["product_template_attribute_value_ids"][0]]["name"]: v for v in nuevas}
        ptav_por_medida = {p["name"]: pid for pid, p in ptav_n.items()}
        assert set(por_medida) == {f["medida"] for f in fs}
        log(f"2. {fam['nombre']}: atributo {attr_id} con {len(val_ids)} valores, plantilla nueva {tid_nuevo}")
        # ── 3. Precios: base 0, extra = precio que tenía ──
        por_precio = {}
        for f in fs: por_precio.setdefault(f["precio"], []).append(ptav_por_medida[f["medida"]])
        for precio, ids in por_precio.items():
            call("product.template.attribute.value", "write", ids, {"price_extra": precio})
        mal = [f["medida"] for f in fs if abs(call("product.product", "read", [por_medida[f["medida"]]["id"]], fields=["lst_price"])[0]["lst_price"] - f["precio"]) > 0.005]
        assert not mal, f"{fam['nombre']}: precios que no cuadran: {mal}"
        # ── 4. Códigos y costos ──
        for f in fs:
            nid = por_medida[f["medida"]]["id"]; nueva_de[f["id"]] = nid
            if f["codigo"]:
                call("product.product", "write", [f["id"]], {"default_code": f"{f['codigo']} (anterior)"})
                call("product.product", "write", [nid], {"default_code": f["codigo"]})
                assert call("product.product", "read", [nid], fields=["default_code"])[0]["default_code"] == f["codigo"], f"No quedó el código {f['codigo']}"
            if f["costo"]:
                call("product.product", "write", [nid], {"standard_price": f["costo"]})
                assert abs(call("product.product", "read", [nid], fields=["standard_price"])[0]["standard_price"] - f["costo"]) < 0.005, f"No quedó el costo de {f['medida']}"
        log(f"3-4. {fam['nombre']}: {len(por_precio)} precios distintos, códigos y costos trasladados")

    # ── 5. Stock ────────────────────────────────────────────────────────────────────
    movidas, cambios_durante = 0, []
    for f in filas:
        ahora = bot.cantidad_en_bodega(f["id"])      # lo que hay AHORA (por si alguien vendió justo durante la migración); no se pierde ninguna unidad
        if ahora != f["stock"]:
            cambios_durante.append((f["familia"], f["medida"], f["stock"], ahora))
        if ahora:
            bot.ajustar_stock(nueva_de[f["id"]], ahora)
            bot.ajustar_stock(f["id"], 0)
            movidas += 1
    log("5. Stock movido en", movidas, "variantes" + (f" | OJO, cambió durante la migración: {cambios_durante}" if cambios_durante else ""))

    # ── 6. Archivar las viejas ──────────────────────────────────────────────────────
    for fam in FAMILIAS:
        for tid in fam["origen"]:
            n = call("product.template", "read", [tid], fields=["name"])[0]["name"]
            call("product.template", "write", [tid], {"name": f"{n} (versión anterior)", "active": False})
    log("6. Plantillas viejas renombradas y archivadas:", sum(len(f["origen"]) for f in FAMILIAS))

    # ── 7. Mapa (se SUMA al que ya hay) y verificación ──────────────────────────────
    mapa = json.load(open(salida_mapa)) if os.path.exists(salida_mapa) else {}
    nuevas_claves = {str(k): v for k, v in nueva_de.items()}
    assert not (set(nuevas_claves) & set(mapa)), "Alguna variante vieja ya estaba en el mapa"
    mapa.update(nuevas_claves)
    json.dump(mapa, open(salida_mapa, "w"), indent=1)
    log("7. Mapa actualizado en", salida_mapa, f"({len(mapa)} entradas en total, {len(nuevas_claves)} nuevas)")
    ids_n = list(nueva_de.values())
    q_viejas = call("stock.quant", "search_read", [["product_id", "in", ids_v], ["location_id.usage", "=", "internal"]], fields=["quantity"])
    q_nuevas = call("stock.quant", "search_read", [["product_id", "in", ids_n], ["location_id.usage", "=", "internal"]], fields=["product_id", "quantity"])
    resumen = {
        "variantes_nuevas": len(ids_n), "plantillas_nuevas": plantillas_nuevas,
        "viejas_archivadas": call("product.product", "search_count", [["id", "in", ids_v], ["active", "=", False]], context={"active_test": False}),
        "viejas_eliminadas": len(set(ids_v) - set(call("product.product", "search", [["id", "in", ids_v]], context={"active_test": False}))),
        "stock_viejas": sum(q["quantity"] for q in q_viejas), "stock_nuevas": sum(q["quantity"] for q in q_nuevas), "stock_antes": sum(f["stock"] for f in filas),
        "stock_por_variante_igual": all(abs(sum(q["quantity"] for q in q_nuevas if q["product_id"][0] == nueva_de[f["id"]]) - f["stock"]) < 1e-6 for f in filas),
        "asientos_nuevos": call("account.move", "search_count", []) - contadores_antes["asientos"],
        "stock_total_diff": sum(q["quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["quantity"])) - contadores_antes["stock_total"],
        "stock_cambio_durante": cambios_durante,
    }
    d_foto = json.load(open(salida_foto)); d_foto["nueva_de"] = nuevas_claves; d_foto["plantillas_nuevas"] = plantillas_nuevas
    json.dump(d_foto, open(salida_foto, "w"), ensure_ascii=False, indent=1)
    log("7. Resumen:", resumen)
    ver = verificar(bot, salida_foto, salida_mapa, salida_csv or salida_foto.replace(".json", "_verificacion.csv"))
    return {"foto": filas, "mapa": mapa, "resumen": resumen, "nueva_de": nueva_de, "plantillas_nuevas": plantillas_nuevas, "verificacion": ver}


def verificar(bot, foto_json, mapa_json, csv_salida):
    """Verificación SOLO LECTURA, variante por variante, contra la foto tomada ANTES de migrar y contra Odoo mismo. Por cada una comprueba: nombre
    esperado «[código] Codo negro (2 1/2, 20)», precio (contra la foto Y contra lo que aún calcula la vieja archivada), costo, código (nuevo = el de antes,
    viejo = «… (anterior)»), stock (nuevo = antes, viejo = 0), variante vieja archivada y mapa viejo→nuevo."""
    uid, models = bot.odoo_connect()
    def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)
    d = json.load(open(foto_json)); mapa = json.load(open(mapa_json)); foto = d["variantes"]
    nuevas = {v["id"]: v for v in call("product.product", "read", list({mapa[str(f["id"])] for f in foto if str(f["id"]) in mapa}), fields=["display_name", "lst_price", "standard_price", "default_code", "active", "product_tmpl_id"])}
    viejas = {v["id"]: v for v in call("product.product", "read", [f["id"] for f in foto], fields=["display_name", "lst_price", "default_code", "active"], context={"active_test": False})}
    plantillas_nuevas = d.get("plantillas_nuevas", {})
    problemas, filas = [], []
    for f in foto:
        nid = mapa.get(str(f["id"])); n = nuevas.get(nid); v = viejas.get(f["id"]); fila_p = []
        etiqueta = f"{f['familia']} {f['medida']}"
        if not n: problemas.append(f"{etiqueta}: sin variante nueva"); continue
        esperado = (f"[{f['codigo']}] " if f["codigo"] else "") + f"{f['familia']} ({f['medida']})"
        if n["display_name"] != esperado: fila_p.append(f"nombre '{n['display_name']}' ≠ '{esperado}'")
        if abs(n["lst_price"] - f["precio"]) > 0.005: fila_p.append(f"precio {n['lst_price']} ≠ {f['precio']}")
        if abs(n["standard_price"] - f["costo"]) > 0.005: fila_p.append(f"costo {n['standard_price']} ≠ {f['costo']}")
        if (n["default_code"] or False) != f["codigo"]: fila_p.append(f"código {n['default_code']!r} ≠ {f['codigo']!r}")
        if not n["active"]: fila_p.append("la nueva NO está activa")
        if v is None or v["active"]: fila_p.append("la variante vieja NO quedó archivada")
        else:
            if abs(v["lst_price"] - n["lst_price"]) > 0.005: fila_p.append(f"precio nuevo {n['lst_price']} ≠ lo que aún calcula la vieja {v['lst_price']}")
            if f["codigo"] and v["default_code"] != f"{f['codigo']} (anterior)": fila_p.append(f"código de la vieja {v['default_code']!r}")
        stock_n = bot.cantidad_en_bodega(nid); stock_v = bot.cantidad_en_bodega(f["id"])
        if abs(stock_n - f["stock"]) > 1e-6: fila_p.append(f"stock nuevo {stock_n:g} ≠ antes {f['stock']:g}")
        if stock_v != 0: fila_p.append(f"stock de la vieja {stock_v:g} ≠ 0")
        if plantillas_nuevas and n["product_tmpl_id"][0] != plantillas_nuevas.get(f["familia"]): fila_p.append("está en otra plantilla")
        filas.append((f["familia"], f["medida"], esperado, f["codigo"], f["precio"], n["lst_price"], f["stock"], stock_n, stock_v, "OK" if not fila_p else "; ".join(fila_p)))
        problemas += [f"{etiqueta}: {x}" for x in fila_p]
    tot_antes = sum(f["stock"] for f in foto); tot_nuevo = sum(r[7] for r in filas)
    if abs(tot_antes - tot_nuevo) > 1e-6: problemas.append(f"stock total {tot_nuevo:g} ≠ {tot_antes:g}")
    if len(filas) != len(foto) or len({mapa.get(str(f['id'])) for f in foto}) != len(foto): problemas.append(f"filas={len(filas)} de {len(foto)}")
    with open(csv_salida, "w", encoding="utf-8-sig") as fh:
        fh.write("familia;medida;nombre;codigo;precio_antes;precio_ahora;stock_antes;stock_ahora_nueva;stock_ahora_vieja;resultado\n")
        for r in sorted(filas, key=lambda r: (r[0], r[1])): fh.write(";".join(str(x) for x in r) + "\n")
    ok = sum(1 for r in filas if r[9] == "OK")
    log(f"VERIFICACIÓN: {ok}/{len(filas)} variantes OK | stock total antes={tot_antes:g} ahora={tot_nuevo:g} | problemas={len(problemas)} | tabla: {csv_salida}")
    for pr in problemas[:20]: log("   ✗", pr)
    return {"filas": len(filas), "ok": ok, "problemas": problemas, "csv": csv_salida}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--destino", choices=["copia", "produccion"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--si-estoy-seguro", action="store_true")
    ap.add_argument("--verificar", help="solo lectura: verifica una migración ya hecha con esta foto JSON (ruta)")
    a = ap.parse_args()
    if a.destino == "copia":
        os.environ["ODOO_URL"] = "http://127.0.0.1:8070"; os.environ["ODOO_DB"] = "radibombas_prueba"
    os.environ["MODO_ENTREGA"] = "odoo"
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
    import logging; logging.disable(logging.CRITICAL)
    import bot
    if a.destino == "copia":
        assert bot.ODOO_DB == "radibombas_prueba" and bot.ODOO_URL.endswith(":8070"), "¡NO está apuntando a la copia!"
        foto, mapa = "/tmp/migracion_codos_copia.json", "/tmp/mapa_variantes_copia.json"
    else:
        import subprocess, glob
        foto, mapa = time.strftime(os.path.join(ROOT, "data", "migracion_codos_%Y%m%d_%H%M.json")), os.path.join(ROOT, "data", "mapa_variantes.json")
        if not a.verificar and not a.dry_run:
            assert a.si_estoy_seguro, "Producción: falta --si-estoy-seguro"
            assert subprocess.run(["systemctl", "is-active", "whatsapp-bot"], capture_output=True, text=True).stdout.strip() != "active", "Detén el bot antes (systemctl stop whatsapp-bot)"
            recientes = [f for f in glob.glob(os.path.join(os.environ.get("RADIBOT_BACKUP_DIR", "/var/backups/radibot/daily"), "*.dump")) if time.time() - os.path.getmtime(f) < 1800]
            assert recientes, "Haz un respaldo ahora (systemctl start respaldo-radibombas.service): no hay uno de los últimos 30 minutos"
    if a.verificar:
        r = verificar(bot, a.verificar, mapa, a.verificar.replace(".json", "_verificacion.csv"))
        sys.exit(0 if not r["problemas"] else 1)
    res = migrar(bot, foto, mapa, dry_run=a.dry_run)
    if not a.dry_run and res["verificacion"]["problemas"]:
        log("✗✗ LA VERIFICACIÓN ENCONTRÓ PROBLEMAS: no sigas; revisa la tabla y, si hace falta, restaura el respaldo."); sys.exit(1)
