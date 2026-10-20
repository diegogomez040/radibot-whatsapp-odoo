# Prueba de precio / código / nombre en productos CON VARIANTES contra la COPIA temporal (puerto 8070).
# Primero:  ./copia_odoo.sh arrancar   Después:  python3 probar_variantes_en_copia.py   Al terminar:  ./copia_odoo.sh parar
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
def prod(cod): return call("product.product", "search_read", [["default_code", "=", cod]], fields=["id", "display_name", "lst_price", "standard_price", "qty_available", "product_tmpl_id", "default_code"], limit=1)[0]
def tmpl(tid): return call("product.template", "read", [tid], fields=["name", "list_price", "default_code"])[0]
def hermanas(tid, excepto): return {p["id"]: p for p in call("product.product", "search_read", [["product_tmpl_id", "=", tid], ["id", "!=", excepto]], fields=["lst_price", "standard_price", "default_code", "display_name"])}
def ptavs(tid): return {p["id"]: p["price_extra"] for p in call("product.template.attribute.value", "search_read", [["product_tmpl_id", "=", tid]], fields=["price_extra"])}
def cuentas(): return {"asientos": call("account.move", "search_count", []), "mov_stock": call("stock.move", "search_count", [])}
try:
    C0 = cuentas()
    V = prod("1237"); T = tmpl(V["product_tmpl_id"][0]); H0 = hermanas(T["id"], V["id"]); PT0 = ptavs(T["id"])
    print(f"Copia lista. 1237: precio {V['lst_price']:,.0f} (base de la plantilla {T['list_price']:,.0f}), {len(H0)} variantes hermanas\n")

    print("=== T1. Precio de UNA variante (1237) con extra propio")
    bot.actualizar_producto(V["id"], nuevo_precio=5000)
    d = prod("1237"); H1 = hermanas(T["id"], V["id"]); PT1 = ptavs(T["id"])
    chk("la 1237 quedó en $5,000 exactos", d["lst_price"] == 5000, str(d["lst_price"]))
    chk("el precio base de la plantilla NO cambió", tmpl(T["id"])["list_price"] == T["list_price"])
    chk("las 12 hermanas conservan su precio", all(H1[i]["lst_price"] == H0[i]["lst_price"] for i in H0))
    cambiados = [i for i in PT1 if PT1[i] != PT0[i]]; chk("solo cambió UN extra de atributo (el de la 1237)", len(cambiados) == 1, str(cambiados))
    bot.actualizar_producto(V["id"], nuevo_precio=3000); chk("bajarlo a $3,000 también", prod("1237")["lst_price"] == 3000 and all(hermanas(T["id"], V["id"])[i]["lst_price"] == H0[i]["lst_price"] for i in H0))
    bot.actualizar_producto(V["id"], nuevo_precio=V["lst_price"]); chk("restablecido a $4,000 (como estaba)", prod("1237")["lst_price"] == 4000 and ptavs(T["id"]) == PT0)

    print("\n=== T2. Producto SIMPLE (5490): sigue usando el precio de la plantilla")
    S = prod("5490"); TS = tmpl(S["product_tmpl_id"][0])
    bot.actualizar_producto(S["id"], nuevo_precio=31000)
    chk("5490 en $31,000 (precio de lista de la plantilla)", prod("5490")["lst_price"] == 31000 and tmpl(TS["id"])["list_price"] == 31000)
    bot.actualizar_producto(S["id"], nuevo_precio=S["lst_price"]); chk("restablecido", prod("5490")["lst_price"] == S["lst_price"])

    print("\n=== T3. Tapa mediana (1 atributo, extras en 0): una variante sube $1,000, las demás no")
    M = call("product.product", "search_read", [["product_tmpl_id.name", "=", "Tapa mediana"]], fields=["id", "lst_price", "product_tmpl_id"], limit=1)[0]; TM = M["product_tmpl_id"][0]
    HM0 = hermanas(TM, M["id"]); bot.actualizar_producto(M["id"], nuevo_precio=M["lst_price"] + 1000); HM1 = hermanas(TM, M["id"])
    chk("sube la elegida y las otras 5 no", call("product.product", "read", [M["id"]], fields=["lst_price"])[0]["lst_price"] == M["lst_price"] + 1000 and all(HM1[i]["lst_price"] == HM0[i]["lst_price"] for i in HM0))
    bot.actualizar_producto(M["id"], nuevo_precio=M["lst_price"]); chk("restablecida", call("product.product", "read", [M["id"]], fields=["lst_price"])[0]["lst_price"] == M["lst_price"])

    print("\n=== T4. Corrugada (2 atributos): NO se puede aislar → error y nada cambia")
    K = call("product.product", "search_read", [["product_tmpl_id.name", "=", "Manguera corrugada"]], fields=["id", "lst_price", "product_tmpl_id"], limit=1)[0]; TK = K["product_tmpl_id"][0]
    antes = {p["id"]: p["lst_price"] for p in call("product.product", "search_read", [["product_tmpl_id", "=", TK]], fields=["lst_price"])}; ex0 = ptavs(TK)
    try:
        bot.actualizar_producto(K["id"], nuevo_precio=K["lst_price"] + 5000); lanzo = None
    except RuntimeError as e:
        lanzo = str(e)
    despues = {p["id"]: p["lst_price"] for p in call("product.product", "search_read", [["product_tmpl_id", "=", TK]], fields=["lst_price"])}
    chk("lanza error explicando por qué", lanzo and "UNA sola variante" in lanzo, (lanzo or "")[:80])
    chk("las 128 variantes y sus extras siguen EXACTAMENTE igual", antes == despues and ex0 == ptavs(TK))

    print("\n=== T5. Código: va en la VARIANTE")
    bot.actualizar_producto(V["id"], nuevo_codigo="1237-T")
    d = prod("1237-T"); chk("la variante tiene el código nuevo", d["id"] == V["id"])
    chk("la plantilla sigue SIN código y las hermanas conservan el suyo", tmpl(T["id"])["default_code"] is False and all(hermanas(T["id"], V["id"])[i]["default_code"] == H0[i]["default_code"] for i in H0))
    bot.actualizar_producto(V["id"], nuevo_codigo="1237"); chk("restablecido a 1237", prod("1237")["id"] == V["id"])
    bot.actualizar_producto(S["id"], nuevo_codigo="5490-T"); chk("producto simple: el código se ve también en la plantilla", tmpl(TS["id"])["default_code"] == "5490-T")
    bot.actualizar_producto(S["id"], nuevo_codigo="5490"); chk("restablecido a 5490", tmpl(TS["id"])["default_code"] == "5490")

    print("\n=== T6. Nombre: es de la PLANTILLA (cambia en todas las variantes, como avisa la vista previa)")
    bot.actualizar_producto(V["id"], nuevo_nombre="Manguera tramo negra X")
    nombres = [p["display_name"] for p in call("product.product", "search_read", [["product_tmpl_id", "=", T["id"]]], fields=["display_name"])]
    chk("las 13 variantes muestran el nombre nuevo", len(nombres) == 13 and all("negra X" in n for n in nombres))
    bot.actualizar_producto(V["id"], nuevo_nombre=T["name"]); chk("restablecido", tmpl(T["id"])["name"] == T["name"])

    print("\n=== T7. Precio + costo + código + stock de una variante, todo en una sola confirmación")
    bot.limpiar_pendiente = lambda u: None
    s0 = prod("1237")["qty_available"]
    msg = bot.ejecutar_actualizacion("u", {"actualizaciones": [{"product_id": V["id"], "product_name": V["display_name"], "cambios": {"nuevo_precio": 4500, "nuevo_costo": 2000, "nuevo_codigo": "1237-Z", "agregar_stock": 1}}]})
    d = prod("1237-Z")
    chk("las cuatro cosas quedaron", d["lst_price"] == 4500 and d["standard_price"] == 2000 and d["qty_available"] == s0 + 1, f"{d['lst_price']} / {d['standard_price']} / stock {s0:.0f}→{d['qty_available']:.0f}")
    chk("las hermanas no se movieron (precio ni costo)", all(hermanas(T["id"], V["id"])[i]["lst_price"] == H0[i]["lst_price"] and hermanas(T["id"], V["id"])[i]["standard_price"] == H0[i]["standard_price"] for i in H0))
    chk("el mensaje lo cuenta", "Precio: $4,500" in msg and "Costo: $2,000" in msg and "Código: 1237-Z" in msg, msg.replace("\n", " | ")[:140])

    print("\n=== T8. Seguridad: si Odoo no deja los precios como se esperaba, se deshace y se avisa")
    real_write = bot.odoo_write
    bot.odoo_write = lambda model, ids, vals: True if model == "product.template.attribute.value" else real_write(model, ids, vals)   # simula un write que no hace nada
    try:
        bot.actualizar_producto(V["id"], nuevo_precio=9000); err = None
    except RuntimeError as e:
        err = str(e)
    bot.odoo_write = real_write
    chk("lanza error y el precio sigue como estaba", err and "deshice" in err and prod("1237-Z")["lst_price"] == 4500, (err or "")[:70])

    # dejar la copia como estaba
    bot.actualizar_producto(V["id"], nuevo_precio=4000, nuevo_codigo="1237", nuevo_costo=V["standard_price"], establecer_stock=int(s0))

    print("\n=== T9. Invariantes")
    C1 = cuentas()
    chk("0 asientos contables nuevos", C1["asientos"] == C0["asientos"])
    chk("la plantilla y todas las hermanas, como al inicio", tmpl(T["id"])["list_price"] == T["list_price"] and all(hermanas(T["id"], V["id"])[i]["lst_price"] == H0[i]["lst_price"] for i in H0) and ptavs(T["id"]) == PT0)
except Exception:
    traceback.print_exc(); R.append(("EXCEPCIÓN en la prueba", False))
malos = [t for t, c in R if not c]
print(f"\n{len(R) - len(malos)}/{len(R)} comprobaciones OK" + ("" if not malos else "  ← FALLARON: " + "; ".join(malos)))
sys.exit(1 if malos else 0)
