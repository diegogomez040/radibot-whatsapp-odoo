# Migración de "Tapa pequeña": de DOS atributos (Tipo de tapa × Libras de tapa, precio = base + extra del tipo + extra de las libras)
# a UN atributo "medida tapa pequeña" con 4 valores "tipo, libras" (Alta, 13 / Alta, 16 / Baja, 13 / Baja, 16), cada uno con su propio extra
# (= precio individual) y el precio base de la plantilla en 0. Es la misma idea que ya se aplicó a las corrugadas (migrar_corrugadas.py), con lo que
# a ellas no les hacía falta: aquí cada variante tiene CÓDIGO propio (TR-27, TR-33, TR-41, TR-42) y puede tener costo, así que también se trasladan.
#
#   python3 migrar_tapa_pequena.py --destino copia [--dry-run]                  (contra la copia temporal de pruebas, puerto 8070)
#   python3 migrar_tapa_pequena.py --destino produccion --si-estoy-seguro       (con el bot detenido y un respaldo de menos de 30 minutos)
#   python3 migrar_tapa_pequena.py --destino produccion --verificar <foto.json> (solo lectura, cuando quieras)
#
# Estrategia (igual que en las corrugadas): se crea una plantilla NUEVA "Tapa pequeña" con el atributo único y se ARCHIVA la vieja completa, con sus
# 4 variantes y todo su historial (no se borra nada; reversible). Orden (cualquier cosa rara aborta ANTES de escribir):
#   0. Comprobaciones: la plantilla tiene exactamente los atributos «Tipo de tapa» y «Libras de tapa», 4 variantes, sin códigos de barras ni foto
#      (si hubiera foto se copia), nada abierto (borradores, movimientos pendientes, reservas, stock negativo), el atributo nuevo no existe todavía.
#   1. Foto de todo (código, precio, costo, stock, nombre) en un JSON.
#   2. Crea el atributo con sus 4 valores y la plantilla nueva (mismos ajustes que la vieja).
#   3. Precio base 0 y extra de cada medida = el precio que tenía (los precios NO cambian).
#   4. Códigos: la variante vieja pasa de "TR-27" a "TR-27 (anterior)" y la nueva recibe "TR-27" (así el bot y Odoo ven un solo producto con ese código).
#      Costo: se copia a la variante nueva si no es 0.
#   5. Mueve el stock a las variantes nuevas (ajustes de inventario con historial) y deja las viejas en 0.
#   6. Renombra la plantilla vieja ("… (versión anterior)") y la ARCHIVA.
#   7. SUMA al mapa variante vieja → nueva (data/mapa_variantes.json; NO borra lo que ya tiene el de las corrugadas) y verifica todo.
import os, sys, json, time, argparse
from collections import Counter

PLANTILLA = "Tapa pequeña"
CATEGORIA_ID = 39                      # Tapas de radiador
ATRIBUTOS = ("Tipo de tapa", "Libras de tapa")
NOMBRE_ATRIBUTO = "medida tapa pequeña"
N_VARIANTES = 4


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def _orden(medida):
    tipo, libras = medida.split(", ", 1)
    try:
        return (tipo, float(libras))
    except ValueError:
        return (tipo, 0.0)


def migrar(bot, salida_foto, salida_mapa, dry_run=False, salida_csv=None):
    uid, models = bot.odoo_connect()
    def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)

    # ── 0. Comprobaciones ─────────────────────────────────────────────────────────────
    plantillas = call("product.template", "search_read", [["name", "=", PLANTILLA], ["categ_id", "=", CATEGORIA_ID], ["active", "=", True]], fields=["id", "list_price", "attribute_line_ids", "image_1920"])
    assert len(plantillas) == 1, f"Debe haber UNA plantilla activa '{PLANTILLA}' en la categoría {CATEGORIA_ID}; hay {len(plantillas)}"
    t = plantillas[0]; tid = t["id"]
    lineas = call("product.template.attribute.line", "read", t["attribute_line_ids"], fields=["attribute_id", "value_ids"])
    assert {l["attribute_id"][1] for l in lineas} == set(ATRIBUTOS), f"Atributos inesperados: {[l['attribute_id'][1] for l in lineas]}"
    assert not call("product.attribute", "search_count", [["name", "=", NOMBRE_ATRIBUTO]]), f"Ya existe el atributo '{NOMBRE_ATRIBUTO}' (¿ya se migró?)"
    viejas = call("product.product", "search_read", [["product_tmpl_id", "=", tid]], fields=["id", "display_name", "lst_price", "standard_price", "default_code", "barcode", "product_template_attribute_value_ids"])
    assert len(viejas) == N_VARIANTES, f"Se esperaban {N_VARIANTES} variantes, hay {len(viejas)}"
    assert not any(v["barcode"] for v in viejas), "Hay códigos de barras en las variantes: este script no los traslada"
    codigos = [v["default_code"] for v in viejas if v["default_code"]]
    assert len(codigos) == len(set(codigos)), "Códigos repetidos entre las variantes"
    ids_v = [v["id"] for v in viejas]
    otros = call("product.product", "search_count", [["default_code", "in", codigos], ["id", "not in", ids_v]]) if codigos else 0
    assert otros == 0, f"Algún código de estas tapas lo usa OTRO producto activo ({otros})"
    refs_antes = Counter(m["product_id"][0] for m in call("stock.move", "search_read", [["product_id", "in", ids_v]], fields=["product_id"]))
    refs_antes.update(l["product_id"][0] for l in call("sale.order.line", "search_read", [["product_id", "in", ids_v]], fields=["product_id"]))
    assert call("sale.order.line", "search_count", [["product_id", "in", ids_v], ["state", "in", ["draft", "sent"]]]) == 0, "Hay órdenes en borrador con estas tapas"
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
        tipo = next(ptav[i]["name"] for i in v["product_template_attribute_value_ids"] if ptav[i]["attribute_id"][1] == ATRIBUTOS[0])
        libras = next(ptav[i]["name"] for i in v["product_template_attribute_value_ids"] if ptav[i]["attribute_id"][1] == ATRIBUTOS[1])
        medida = f"{tipo}, {libras}"
        esperado = (f"[{v['default_code']}] " if v["default_code"] else "") + f"{PLANTILLA} ({medida})"
        assert v["display_name"] == esperado, f"Nombre inesperado: {v['display_name']!r} (esperaba {esperado!r})"
        foto.append({"id": v["id"], "nombre": v["display_name"], "medida": medida, "codigo": v["default_code"] or False, "precio": v["lst_price"],
                     "costo": v["standard_price"], "stock": stock_por_variante.get(v["id"], 0)})
    assert len({f["medida"] for f in foto}) == N_VARIANTES, "Las medidas deben ser únicas"
    json.dump({"plantilla_id": tid, "list_price": t["list_price"], "variantes": foto}, open(salida_foto, "w"), ensure_ascii=False, indent=1)
    log("1. Foto guardada en", salida_foto)
    if dry_run:
        log("DRY-RUN: no se escribió nada en Odoo."); return {"foto": foto}

    contadores_antes = {"asientos": call("account.move", "search_count", []), "stock_total": sum(q["quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["quantity"]))}

    # ── 2. Atributo nuevo y plantilla nueva ──────────────────────────────────────────
    orden = sorted(foto, key=lambda f: _orden(f["medida"]))
    attr_id = call("product.attribute", "create", {"name": NOMBRE_ATRIBUTO, "display_type": "select", "create_variant": "always"})
    val_ids = call("product.attribute.value", "create", [{"name": f["medida"], "attribute_id": attr_id, "sequence": i + 1} for i, f in enumerate(orden)])
    log("2a. Atributo", attr_id, "con", len(val_ids), "valores")
    campos = call("product.template", "fields_get", attributes=["type"])
    quiero = [c for c in ("categ_id", "type", "is_storable", "tracking", "uom_id", "taxes_id", "supplier_taxes_id", "sale_ok", "purchase_ok",
                          "description", "description_sale", "description_purchase", "invoice_policy", "service_type", "sequence", "image_1920") if c in campos]
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
    tid_nuevo = call("product.template", "create", ajustes)
    log(f"2b. Plantilla nueva {tid_nuevo} creada")
    nuevas = call("product.product", "search_read", [["product_tmpl_id", "=", tid_nuevo]], fields=["id", "display_name", "product_template_attribute_value_ids"])
    assert len(nuevas) == N_VARIANTES, f"Se esperaban {N_VARIANTES} variantes nuevas activas; hay {len(nuevas)}"
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
    assert not mal, f"Precios que no cuadran: {mal}"

    # ── 4. Códigos y costos ─────────────────────────────────────────────────────────
    for f in foto:
        nid = nueva_por_medida[f["medida"]]["id"]
        if f["codigo"]:
            call("product.product", "write", [f["id"]], {"default_code": f"{f['codigo']} (anterior)"})
            call("product.product", "write", [nid], {"default_code": f["codigo"]})
            assert call("product.product", "read", [nid], fields=["default_code"])[0]["default_code"] == f["codigo"], f"No quedó el código {f['codigo']}"
        if f["costo"]:
            call("product.product", "write", [nid], {"standard_price": f["costo"]})
            assert abs(call("product.product", "read", [nid], fields=["standard_price"])[0]["standard_price"] - f["costo"]) < 0.005, f"No quedó el costo de {f['medida']}"
    log("4. Códigos y costos trasladados")

    # ── 5. Stock ────────────────────────────────────────────────────────────────────
    movidas, cambios_durante = 0, []
    for f in foto:
        ahora = bot.cantidad_en_bodega(f["id"])      # lo que hay AHORA (por si alguien vendió justo durante la migración); no se pierde ninguna unidad
        if ahora != f["stock"]:
            cambios_durante.append((f["medida"], f["stock"], ahora))
        if ahora:
            bot.ajustar_stock(nueva_por_medida[f["medida"]]["id"], ahora)
            bot.ajustar_stock(f["id"], 0)
            movidas += 1
    log("5. Stock movido en", movidas, "medidas" + (f" | OJO, cambió durante la migración: {cambios_durante}" if cambios_durante else ""))

    # ── 6. Archivar la vieja ────────────────────────────────────────────────────────
    call("product.template", "write", [tid], {"name": f"{PLANTILLA} (versión anterior)", "active": False})
    log("6. Plantilla vieja renombrada y archivada")

    # ── 7. Mapa (se SUMA al que ya hay) y verificación ──────────────────────────────
    mapa = json.load(open(salida_mapa)) if os.path.exists(salida_mapa) else {}
    nuevas_claves = {str(f["id"]): nueva_por_medida[f["medida"]]["id"] for f in foto}
    assert not (set(nuevas_claves) & set(mapa)), "Alguna variante vieja ya estaba en el mapa"
    mapa.update(nuevas_claves)
    json.dump(mapa, open(salida_mapa, "w"), indent=1)
    log("7. Mapa actualizado en", salida_mapa, f"({len(mapa)} entradas en total, {len(nuevas_claves)} nuevas)")
    q_viejas = call("stock.quant", "search_read", [["product_id", "in", ids_v], ["location_id.usage", "=", "internal"]], fields=["quantity"])
    q_nuevas = call("stock.quant", "search_read", [["product_id", "in", [v["id"] for v in nuevas]], ["location_id.usage", "=", "internal"]], fields=["product_id", "quantity"])
    resumen = {
        "variantes_nuevas": len(nuevas),
        "plantillas_activas_con_ese_nombre": call("product.template", "search_count", [["name", "=", PLANTILLA], ["active", "=", True]]),
        "viejas_archivadas": call("product.product", "search_count", [["id", "in", ids_v], ["active", "=", False]], context={"active_test": False}),
        "viejas_eliminadas": len(set(ids_v) - set(call("product.product", "search", [["id", "in", ids_v]], context={"active_test": False}))),
        "stock_viejas": sum(q["quantity"] for q in q_viejas), "stock_nuevas": sum(q["quantity"] for q in q_nuevas), "stock_antes": sum(f["stock"] for f in foto),
        "stock_por_medida_igual": all(abs(sum(q["quantity"] for q in q_nuevas if q["product_id"][0] == nueva_por_medida[f["medida"]]["id"]) - f["stock"]) < 1e-6 for f in foto),
        "asientos_nuevos": call("account.move", "search_count", []) - contadores_antes["asientos"],
        "stock_total_diff": sum(q["quantity"] for q in call("stock.quant", "search_read", [["location_id.usage", "=", "internal"]], fields=["quantity"])) - contadores_antes["stock_total"],
        "plantilla_nueva_id": tid_nuevo, "stock_cambio_durante": cambios_durante,
    }
    d_foto = json.load(open(salida_foto)); d_foto["plantilla_nueva_id"] = tid_nuevo; json.dump(d_foto, open(salida_foto, "w"), ensure_ascii=False, indent=1)
    log("7. Resumen:", resumen)
    ver = verificar(bot, salida_foto, salida_mapa, salida_csv or salida_foto.replace(".json", "_verificacion.csv"))
    return {"foto": foto, "mapa": mapa, "resumen": resumen, "nueva_por_medida": nueva_por_medida, "tmpl_nuevo": tid_nuevo, "tmpl_viejo": tid, "verificacion": ver}


def verificar(bot, foto_json, mapa_json, csv_salida):
    """Verificación SOLO LECTURA, medida por medida, contra la foto tomada ANTES de migrar y contra Odoo mismo. Por cada medida comprueba: nombre
    (con su código), precio (contra la foto Y contra lo que aún calcula la vieja archivada), costo, código (nuevo = el de antes, viejo = "… (anterior)"),
    stock (nuevo = antes, viejo = 0), variante vieja archivada y mapa viejo→nuevo."""
    uid, models = bot.odoo_connect()
    def call(model, method, *a, **k): return models.execute_kw(bot.ODOO_DB, uid, bot.ODOO_PASSWORD, model, method, list(a), k)
    d = json.load(open(foto_json)); mapa = json.load(open(mapa_json)); foto = d["variantes"]
    nuevas = {v["id"]: v for v in call("product.product", "read", list(set(mapa[str(f["id"])] for f in foto if str(f["id"]) in mapa)), fields=["display_name", "lst_price", "standard_price", "default_code", "active", "product_tmpl_id"])}
    viejas = {v["id"]: v for v in call("product.product", "read", [f["id"] for f in foto], fields=["display_name", "lst_price", "default_code", "active"], context={"active_test": False})}
    problemas, filas = [], []
    for f in foto:
        nid = mapa.get(str(f["id"])); n = nuevas.get(nid); v = viejas.get(f["id"]); fila_p = []
        if not n: problemas.append(f"{f['medida']}: sin variante nueva"); continue
        if n["display_name"] != f["nombre"]: fila_p.append(f"nombre '{n['display_name']}' ≠ '{f['nombre']}'")
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
        if n["product_tmpl_id"][0] != d.get("plantilla_nueva_id", n["product_tmpl_id"][0]): fila_p.append("está en otra plantilla")
        filas.append((f["medida"], f["nombre"], f["codigo"], f["precio"], n["lst_price"], f["stock"], stock_n, stock_v, "OK" if not fila_p else "; ".join(fila_p)))
        problemas += [f"{f['medida']}: {x}" for x in fila_p]
    tot_antes = sum(f["stock"] for f in foto); tot_nuevo = sum(r[6] for r in filas)
    if abs(tot_antes - tot_nuevo) > 1e-6: problemas.append(f"stock total {tot_nuevo:g} ≠ {tot_antes:g}")
    if len(filas) != N_VARIANTES or len({mapa.get(str(f['id'])) for f in foto}) != N_VARIANTES: problemas.append(f"filas={len(filas)}")
    with open(csv_salida, "w", encoding="utf-8-sig") as fh:
        fh.write("medida;nombre;codigo;precio_antes;precio_ahora;stock_antes;stock_ahora_nueva;stock_ahora_vieja;resultado\n")
        for r in sorted(filas, key=lambda r: r[0]): fh.write(";".join(str(x) for x in r) + "\n")
    ok = sum(1 for r in filas if r[8] == "OK")
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
        foto, mapa = "/tmp/migracion_tapa_copia.json", "/tmp/mapa_variantes_copia.json"
    else:
        import subprocess, glob
        foto, mapa = time.strftime(os.path.join(ROOT, "data", "migracion_tapa_pequena_%Y%m%d_%H%M.json")), os.path.join(ROOT, "data", "mapa_variantes.json")
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
