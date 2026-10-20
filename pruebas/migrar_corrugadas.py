# Migración de "Manguera corrugada": de DOS atributos (diámetro × largo, precio = base + extra del diámetro + extra del largo, sin precios individuales)
# a UN atributo "medida corrugada" con 128 valores "diámetro, largo" (cada medida con su propio extra = su precio individual, base en 0).
#
#   python3 migrar_corrugadas.py --destino copia [--dry-run]       (contra la copia temporal de pruebas, puerto 8070)
#   python3 migrar_corrugadas.py --destino produccion --si-estoy-seguro   (SOLO de noche, con el bot detenido y respaldo reciente: ver comprobaciones)
#
# Estrategia: se crea una plantilla NUEVA "Manguera corrugada" (copia de los ajustes de la vieja) con el atributo único y se ARCHIVA la vieja completa
# (con sus 128 variantes y todo su historial; no se borra ninguna). Es reversible: reactivar la vieja, archivar la nueva y devolver el stock.
#
# Qué hace (en este orden; cualquier cosa rara aborta ANTES de escribir):
#   0. Comprobaciones: la plantilla tiene exactamente los atributos diámetro (8) y largo (16); nada abierto (órdenes en borrador, movimientos pendientes,
#      reservas); no existe ya el atributo nuevo.
#   1. Foto de todo (precios, stock, nombres) en un JSON.
#   2. Crea el atributo "medida corrugada" con sus 128 valores y una plantilla NUEVA con él (mismos ajustes que la vieja: categoría, tipo, impuestos…).
#   3. Precio base de la plantilla nueva = 0 y extra de cada medida = el precio que tenía (los precios NO cambian).
#   4. Mueve el stock de cada medida a su variante nueva (ajustes de inventario con historial) y deja las viejas en 0.
#   5. Renombra la plantilla vieja ("… (versión anterior)") y la ARCHIVA (sus 128 variantes quedan archivadas, con su historial).
#   6. Guarda el mapa variante vieja → variante nueva (lo usa el bot para devoluciones de ventas anteriores) y verifica todo.
import os, sys, json, time, argparse
from collections import Counter

NOMBRE_ATRIBUTO = "medida corrugada"
PLANTILLA = "Manguera corrugada"
CATEGORIA_ID = 88   # Mangueras / Corrugadas


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def migrar(bot, salida_foto, salida_mapa, dry_run=False, salida_csv=None):
    uid, models = bot.odoo_connect()
    def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)

    # ── 0. Comprobaciones ─────────────────────────────────────────────────────────────
    plantillas = call("product.template", "search_read", [["name", "=", PLANTILLA], ["categ_id", "=", CATEGORIA_ID], ["active", "=", True]], fields=["id", "list_price", "attribute_line_ids", "product_variant_count"])
    assert len(plantillas) == 1, f"Debe haber UNA plantilla activa '{PLANTILLA}' en la categoría {CATEGORIA_ID}; hay {len(plantillas)}"
    t = plantillas[0]; tid = t["id"]
    lineas = call("product.template.attribute.line", "read", t["attribute_line_ids"], fields=["attribute_id", "value_ids"])
    por_attr = {l["attribute_id"][1]: l for l in lineas}
    assert set(por_attr) == {"diámetro corrugada", "longitud corrugada"}, f"Atributos inesperados: {list(por_attr)}"
    assert len(por_attr["diámetro corrugada"]["value_ids"]) == 8 and len(por_attr["longitud corrugada"]["value_ids"]) == 16, "Se esperaban 8 diámetros y 16 largos"
    assert not call("product.attribute", "search_count", [["name", "=", NOMBRE_ATRIBUTO]]), f"Ya existe el atributo '{NOMBRE_ATRIBUTO}' (¿ya se migró?)"
    viejas = call("product.product", "search_read", [["product_tmpl_id", "=", tid]], fields=["id", "display_name", "lst_price", "standard_price", "default_code", "barcode", "product_template_attribute_value_ids"])
    assert len(viejas) == 128, f"Se esperaban 128 variantes, hay {len(viejas)}"
    assert not any(v["default_code"] or v["barcode"] or v["standard_price"] for v in viejas), "Hay códigos, códigos de barras o costos en las variantes: este script no los traslada"
    ids_v = [v["id"] for v in viejas]
    refs_antes = Counter(m["product_id"][0] for m in call("stock.move", "search_read", [["product_id", "in", ids_v]], fields=["product_id"]))
    refs_antes.update(l["product_id"][0] for l in call("sale.order.line", "search_read", [["product_id", "in", ids_v]], fields=["product_id"]))
    assert call("sale.order.line", "search_count", [["product_id", "in", ids_v], ["state", "in", ["draft", "sent"]]]) == 0, "Hay órdenes en borrador con estas mangueras"
    assert call("stock.move", "search_count", [["product_id", "in", ids_v], ["state", "not in", ["done", "cancel"]]]) == 0, "Hay movimientos pendientes"
    quants = call("stock.quant", "search_read", [["product_id", "in", ids_v], ["location_id.usage", "=", "internal"]], fields=["product_id", "quantity", "reserved_quantity"])
    assert not any(q["reserved_quantity"] for q in quants), "Hay unidades reservadas"
    assert not any(q["quantity"] < 0 for q in quants), "Hay stock negativo"
    log("0. Comprobaciones OK:", len(viejas), "variantes,", sum(1 for q in quants if q["quantity"]), "con stock,", f"{sum(q['quantity'] for q in quants):.0f} unidades")

    # ── 1. Foto ───────────────────────────────────────────────────────────────────────
    ptav = {p["id"]: p for p in call("product.template.attribute.value", "search_read", [["product_tmpl_id", "=", tid]], fields=["attribute_id", "name", "price_extra"])}
    stock_por_variante = {}
    for q in quants: stock_por_variante[q["product_id"][0]] = stock_por_variante.get(q["product_id"][0], 0) + q["quantity"]
    foto = []
    for v in viejas:
        d = next(ptav[i]["name"] for i in v["product_template_attribute_value_ids"] if ptav[i]["attribute_id"][1] == "diámetro corrugada")
        l = next(ptav[i]["name"] for i in v["product_template_attribute_value_ids"] if ptav[i]["attribute_id"][1] == "longitud corrugada")
        foto.append({"id": v["id"], "nombre": v["display_name"], "diametro": d, "largo": l, "medida": f"{d}, {l}", "precio": v["lst_price"], "stock": stock_por_variante.get(v["id"], 0)})
    assert len({f["medida"] for f in foto}) == 128, "Las medidas deben ser únicas"
    assert all(f["nombre"] == f"{PLANTILLA} ({f['medida']})" for f in foto), "El nombre de las variantes no sigue '(diámetro, largo)'"
    json.dump({"plantilla_id": tid, "list_price": t["list_price"], "variantes": foto}, open(salida_foto, "w"), ensure_ascii=False, indent=1)
    log("1. Foto guardada en", salida_foto)
    if dry_run:
        log("DRY-RUN: no se escribió nada en Odoo."); return {"foto": foto}

    contadores_antes = {"asientos": call("account.move", "search_count", []), "stock_total": sum(q["quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["quantity"]))}

    # ── 2. Atributo nuevo y plantilla nueva ──────────────────────────────────────────
    def clave(f):
        def num(s_):
            p = s_.split(); a = float(p[0]); b = 0.0
            if len(p) > 1: n_, d_ = p[1].split("/"); b = float(n_) / float(d_)
            return a + b
        return (num(f["diametro"]), num(f["largo"]))
    orden = sorted(foto, key=clave)
    attr_id = call("product.attribute", "create", {"name": NOMBRE_ATRIBUTO, "display_type": "select", "create_variant": "always"})
    val_ids = call("product.attribute.value", "create", [{"name": f["medida"], "attribute_id": attr_id, "sequence": i + 1} for i, f in enumerate(orden)])
    log("2a. Atributo", attr_id, "con", len(val_ids), "valores")
    # ajustes de la vieja que la nueva debe repetir (categoría, tipo, impuestos, unidades, descripciones…)
    campos = call("product.template", "fields_get", attributes=["type"])
    quiero = [c for c in ("categ_id", "type", "is_storable", "tracking", "uom_id", "taxes_id", "supplier_taxes_id", "sale_ok", "purchase_ok",
                          "description", "description_sale", "description_purchase", "invoice_policy", "service_type", "sequence") if c in campos]
    viejo = call("product.template", "read", [tid], fields=quiero)[0]
    ajustes = {}
    for c in quiero:
        v = viejo[c]
        if campos[c]["type"] == "many2one":
            if v: ajustes[c] = v[0]
        elif campos[c]["type"] in ("many2many", "one2many"):
            ajustes[c] = [(6, 0, v)]
        elif v is not False or campos[c]["type"] == "boolean":
            ajustes[c] = v
    ajustes.update({"name": PLANTILLA, "list_price": 0, "active": True,
                    "attribute_line_ids": [(0, 0, {"attribute_id": attr_id, "value_ids": [(6, 0, val_ids)]})]})
    t0 = time.time()
    tid_nuevo = call("product.template", "create", ajustes)
    log(f"2b. Plantilla nueva {tid_nuevo} creada ({time.time() - t0:.0f} s)")
    nuevas = call("product.product", "search_read", [["product_tmpl_id", "=", tid_nuevo]], fields=["id", "display_name", "product_template_attribute_value_ids"])
    assert len(nuevas) == 128, f"Se esperaban 128 variantes nuevas activas; hay {len(nuevas)}"
    ptav_nuevos = {p["id"]: p for p in call("product.template.attribute.value", "search_read", [["product_tmpl_id", "=", tid_nuevo], ["attribute_id", "=", attr_id]], fields=["name", "price_extra"])}
    nueva_por_medida = {ptav_nuevos[v["product_template_attribute_value_ids"][0]]["name"]: v for v in nuevas}
    ptav_por_medida = {p["name"]: pid for pid, p in ptav_nuevos.items()}
    assert set(nueva_por_medida) == {f["medida"] for f in foto}

    # ── 3. Precios: base 0, extra = precio que tenía ─────────────────────────────────
    por_precio = {}
    for f in foto: por_precio.setdefault(f["precio"], []).append(ptav_por_medida[f["medida"]])
    for precio, ids in por_precio.items():
        call("product.template.attribute.value", "write", ids, {"price_extra": precio})
    log("3. Precios cargados:", len(por_precio), "precios distintos")
    mal = [f["medida"] for f in foto if abs(call("product.product", "read", [nueva_por_medida[f["medida"]]["id"]], fields=["lst_price"])[0]["lst_price"] - f["precio"]) > 0.005]
    assert not mal, f"Precios que no cuadran: {mal[:5]}"

    # ── 4. Stock ────────────────────────────────────────────────────────────────────
    movidas, cambios_durante = 0, []
    for f in foto:
        ahora = bot.cantidad_en_bodega(f["id"])      # lo que hay AHORA (por si alguien vendió justo durante la migración); no se pierde ninguna unidad
        if ahora != f["stock"]:
            cambios_durante.append((f["medida"], f["stock"], ahora))
        if ahora:
            bot.ajustar_stock(nueva_por_medida[f["medida"]]["id"], ahora)
            bot.ajustar_stock(f["id"], 0)
            movidas += 1
    log("4. Stock movido en", movidas, "medidas" + (f" | OJO, cambió durante la migración: {cambios_durante}" if cambios_durante else ""))
    call("product.template", "write", [tid], {"name": f"{PLANTILLA} (versión anterior)", "active": False})
    log("5a. Plantilla vieja renombrada y archivada")

    # ── 5. Mapa y verificación ──────────────────────────────────────────────────────
    mapa = {str(f["id"]): nueva_por_medida[f["medida"]]["id"] for f in foto}
    json.dump(mapa, open(salida_mapa, "w"), indent=1)
    log("6. Mapa guardado en", salida_mapa)
    q_viejas = call("stock.quant", "search_read", [["product_id", "in", ids_v], ["location_id.usage", "=", "internal"]], fields=["quantity"])
    q_nuevas = call("stock.quant", "search_read", [["product_id", "in", [v["id"] for v in nuevas]], ["location_id.usage", "=", "internal"]], fields=["product_id", "quantity"])
    antes_total = sum(f["stock"] for f in foto)
    resumen = {
        "variantes_nuevas": len(nuevas),
        "plantillas_activas_con_ese_nombre": call("product.template", "search_count", [["name", "=", PLANTILLA], ["active", "=", True]]),
        "viejas_archivadas": call("product.product", "search_count", [["id", "in", ids_v], ["active", "=", False]], context={"active_test": False}),
        "viejas_eliminadas": len(set(ids_v) - set(call("product.product", "search", [["id", "in", ids_v]], context={"active_test": False}))),
        "eliminadas_con_historial": sum(1 for i in set(ids_v) - set(call("product.product", "search", [["id", "in", ids_v]], context={"active_test": False})) if refs_antes[i]),
        "archivadas_sin_historial": sum(1 for i in call("product.product", "search", [["id", "in", ids_v], ["active", "=", False]], context={"active_test": False}) if not refs_antes[i]),
        "stock_viejas": sum(q["quantity"] for q in q_viejas), "stock_nuevas": sum(q["quantity"] for q in q_nuevas), "stock_antes": antes_total,
        "stock_por_medida_igual": all(abs(sum(q["quantity"] for q in q_nuevas if q["product_id"][0] == nueva_por_medida[f["medida"]]["id"]) - f["stock"]) < 1e-6 for f in foto),
        "asientos_nuevos": call("account.move", "search_count", []) - contadores_antes["asientos"],
        "stock_total_diff": sum(q["quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["quantity"])) - contadores_antes["stock_total"],
    }
    d_foto = json.load(open(salida_foto)); d_foto["plantilla_nueva_id"] = tid_nuevo; json.dump(d_foto, open(salida_foto, "w"), ensure_ascii=False, indent=1)
    resumen["plantilla_nueva_id"] = tid_nuevo
    resumen["stock_cambio_durante"] = cambios_durante
    log("6. Resumen:", resumen)
    ver = verificar(bot, salida_foto, salida_mapa, salida_csv or salida_foto.replace(".json", "_verificacion.csv"))
    return {"foto": foto, "mapa": mapa, "resumen": resumen, "nueva_por_medida": nueva_por_medida, "tmpl_nuevo": tid_nuevo, "tmpl_viejo": tid, "verificacion": ver}


def verificar(bot, foto_json, mapa_json, csv_salida):
    """Verificación SOLO LECTURA, medida por medida, contra la foto tomada ANTES de migrar y contra Odoo mismo. Escribe una tabla CSV (128 filas) y
    devuelve {"filas": 128, "ok": n, "problemas": [...]}. Comprueba por cada medida: nombre, precio (contra la foto Y contra el precio que sigue
    calculando la variante vieja archivada), stock (nuevo = antes, viejo = 0), variante vieja archivada y mapa viejo→nuevo."""
    uid, models = bot.odoo_connect()
    def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)
    d = json.load(open(foto_json)); mapa = json.load(open(mapa_json)); foto = d["variantes"]
    nuevas = {v["id"]: v for v in call("product.product", "read", list(set(mapa.values())), fields=["display_name", "lst_price", "active", "product_tmpl_id", "product_template_attribute_value_ids"])}
    viejas = {v["id"]: v for v in call("product.product", "read", [f["id"] for f in foto], fields=["display_name", "lst_price", "active"], context={"active_test": False})}
    problemas, filas = [], []
    for f in foto:
        nid = mapa.get(str(f["id"])); n = nuevas.get(nid); v = viejas.get(f["id"]); fila_p = []
        if not n: problemas.append(f"{f['medida']}: sin variante nueva"); continue
        if n["display_name"] != f["nombre"]: fila_p.append(f"nombre '{n['display_name']}' ≠ '{f['nombre']}'")
        if abs(n["lst_price"] - f["precio"]) > 0.005: fila_p.append(f"precio {n['lst_price']} ≠ {f['precio']}")
        if v is None or v["active"]: fila_p.append("la variante vieja NO quedó archivada")
        elif abs(v["lst_price"] - n["lst_price"]) > 0.005: fila_p.append(f"precio nuevo {n['lst_price']} ≠ lo que aún calcula la vieja {v['lst_price']}")
        stock_n = bot.cantidad_en_bodega(nid); stock_v = bot.cantidad_en_bodega(f["id"])
        if abs(stock_n - f["stock"]) > 1e-6: fila_p.append(f"stock nuevo {stock_n:g} ≠ antes {f['stock']:g}")
        if stock_v != 0: fila_p.append(f"stock de la vieja {stock_v:g} ≠ 0")
        if n["product_tmpl_id"][0] != d.get("plantilla_nueva_id", n["product_tmpl_id"][0]): fila_p.append("está en otra plantilla")
        filas.append((f["medida"], f["nombre"], f["precio"], n["lst_price"], f["stock"], stock_n, stock_v, "OK" if not fila_p else "; ".join(fila_p)))
        problemas += [f"{f['medida']}: {x}" for x in fila_p]
    tot_antes = sum(f["stock"] for f in foto); tot_nuevo = sum(r[5] for r in filas)
    if abs(tot_antes - tot_nuevo) > 1e-6: problemas.append(f"stock total {tot_nuevo:g} ≠ {tot_antes:g}")
    if len(filas) != 128 or len(set(mapa.values())) != 128: problemas.append(f"filas={len(filas)}, variantes nuevas distintas={len(set(mapa.values()))}")
    with open(csv_salida, "w", encoding="utf-8-sig") as fh:
        fh.write("medida;nombre;precio_antes;precio_ahora;stock_antes;stock_ahora_nueva;stock_ahora_vieja;resultado\n")
        for r in sorted(filas, key=lambda r: r[0]): fh.write(";".join(str(x) for x in r) + "\n")
    ok = sum(1 for r in filas if r[7] == "OK")
    log(f"VERIFICACIÓN: {ok}/{len(filas)} medidas OK | stock total antes={tot_antes:g} ahora={tot_nuevo:g} | problemas={len(problemas)} | tabla: {csv_salida}")
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
        foto, mapa = "/tmp/migracion_corrugadas_copia.json", "/tmp/mapa_variantes_copia.json"
    else:
        import subprocess, glob
        assert a.si_estoy_seguro, "Producción: falta --si-estoy-seguro"
        assert subprocess.run(["systemctl", "is-active", "whatsapp-bot"], capture_output=True, text=True).stdout.strip() != "active", "Detén el bot antes (systemctl stop whatsapp-bot)"
        recientes = [f for f in glob.glob(os.path.join(os.environ.get("RADIBOT_BACKUP_DIR", "/var/backups/radibot/daily"), "*.dump")) if time.time() - os.path.getmtime(f) < 1800]
        assert recientes, "Haz un respaldo ahora (systemctl start respaldo-radibombas.service): no hay uno de los últimos 30 minutos"
        foto, mapa = time.strftime(os.path.join(ROOT, "data", "migracion_corrugadas_%Y%m%d_%H%M.json")), os.path.join(ROOT, "data", "mapa_variantes.json")
    if a.verificar:
        r = verificar(bot, a.verificar, mapa, a.verificar.replace(".json", "_verificacion.csv"))
        sys.exit(0 if not r["problemas"] else 1)
    res = migrar(bot, foto, mapa, dry_run=a.dry_run)
    if not a.dry_run and res["verificacion"]["problemas"]:
        log("✗✗ LA VERIFICACIÓN ENCONTRÓ PROBLEMAS: no sigas; revisa la tabla y, si hace falta, restaura el respaldo."); sys.exit(1)
