# Prueba de la LISTA DE PEDIDOS AL PROVEEDOR (antes «demanda perdida»). Lecturas REALES de Odoo (producción), escrituras de Odoo BLOQUEADAS,
# base de pedidos TEMPORAL (la real no se toca), modelo real. Uso:  python3 pruebas/probar_lista_pedidos.py
import os, sys, copy, tempfile, sqlite3, time
from pathlib import Path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
import logging; logging.disable(logging.CRITICAL)
import bot

TMP = Path(tempfile.mkdtemp()) / "pedidos.db"
mem = {}
bot.obtener_pendiente = lambda u: copy.deepcopy(mem.get(u)); bot.guardar_pendiente = lambda u, a: mem.__setitem__(u, copy.deepcopy(a)); bot.limpiar_pendiente = lambda u: mem.pop(u, None)
def _bloq(*a, **k): raise RuntimeError("¡ESCRITURA REAL bloqueada en la prueba!")
bot.odoo_write = _bloq; bot.ajustar_stock = _bloq; bot.odoo_create = _bloq; bot.registrar_venta_completa = _bloq
bot.actualizar_producto = lambda pid, **c: True
R = []
def chk(t, c, extra=""): R.append((t, bool(c))); print(f"  {'OK ' if c else 'MAL'} {t} {extra}")

def base_limpia():
    bot.DEMAND_DB = TMP
    TMP.unlink(missing_ok=True)
    bot.init_demand_db(); bot.migrar_demanda_legacy()
    bot.OFERTA_FALTANTE.clear(); bot.LISTA_PEDIDOS.clear(); bot.MEDIDA_PREGUNTADA.clear(); mem.clear()
def filas(sql="SELECT id, nombre, estado, product_id, motivo, clave FROM demanda ORDER BY id"):
    c = sqlite3.connect(TMP); r = c.execute(sql).fetchall(); c.close(); return r

print("##### A. Base de datos y reglas (sin modelo)")
base_limpia()
v, d = bot.anotar_faltante("manguera corrugada 1 1/2 x 28", "mangueras corrugadas", None, 1, None, "no_existe")
chk("anota un pedido nuevo", (v, d) == (1, False) and len(filas()) == 1)
v, d = bot.anotar_faltante("manguera corrugada 1 1/2 x 28", "mangueras corrugadas", None, 1, None, "no_existe")
chk("el MISMO aviso segundos después NO se duplica", d and len(filas()) == 1)
c = sqlite3.connect(TMP); c.execute("UPDATE demanda SET fecha=?", ((bot.ahora_utc() - bot.timedelta(minutes=30)).isoformat(),)); c.commit(); c.close()
v, d = bot.anotar_faltante("Manguera corrugada de 1 1/2 por 28", None, None, 1, None, "explicito")
chk("una hora distinta, dicho de otra forma ('de … por') → mismo producto: ya van 2 pedidos", (v, d) == (2, False), f"veces={v}")
chk("la clave ignora 'de/por/x', el orden y el plural", bot.clave_faltante("corrugadas 28 por 1 1/2 manguera") == bot.clave_faltante("manguera corrugada de 1 1/2 x 28"))
bot.anotar_faltante("radiador Tesla", "radiadores", None, 3, None, "explicito")
pend, ped = bot.grupos_faltantes()
chk("la lista agrupa por producto y pone primero lo MÁS pedido", [g["veces"] for g in pend] == [2, 1] and pend[0]["nombre"].lower().startswith("manguera corrugada"), str([(g["nombre"][:25], g["veces"], g["unidades"]) for g in pend]))
chk("las unidades se suman (radiador Tesla ×3)", pend[1]["unidades"] == 3)
txt = bot.texto_lista_pedidos("u")
chk("el texto muestra numerada la lista y cómo actualizarla", "1. " in txt and "2. " in txt and "ya pedí el 1" in txt and "quita el 2" in txt and "hoy" in txt, txt.replace("\n", " | ")[:200])
r = bot.atajo_lista_pedidos("u", "ya pedí el 1")
chk("«ya pedí el 1» → pasa a «ya pedidos»", r and "Marcados como pedidos" in r and len(bot.grupos_faltantes()[1]) == 1, r.replace("\n", " | ") if r else "None")
txt = bot.texto_lista_pedidos("u")
chk("la lista ahora separa «Ya pedidos»", "Ya pedidos" in txt and "pedido hoy" in txt)
v, d = bot.anotar_faltante("manguera corrugada 1 1/2 x 28", None, None, 1, None, "no_existe")
chk("si llega OTRO cliente pidiendo lo ya pedido, vuelve a pendientes con su pedido nuevo", len(bot.grupos_faltantes()[0]) == 2 and v == 1, f"veces={v}")
txt = bot.texto_lista_pedidos("u"); r = bot.atajo_lista_pedidos("u", "quita el 1")
chk("«quita el 1» lo saca de la lista", r and "Quité de la lista" in r and len(bot.grupos_faltantes()[0]) + len(bot.grupos_faltantes()[1]) == 1, r.replace("\n", " | ") if r else "None")
bot.texto_lista_pedidos("u")
chk("número que no está → lo dice, no toca nada", "No hay un 9" in (bot.atajo_lista_pedidos("u", "quita el 9") or ""))
chk("«llegaron 5» NO toca la lista (es stock que entró)", bot.atajo_lista_pedidos("u", "llegaron 5") is None)
chk("«sácame la foto del 2» / «foto del 2» NO toca la lista", bot.atajo_lista_pedidos("u", "quita la foto del 2") is None and bot.atajo_lista_pedidos("u", "foto del 2") is None)
chk("una pregunta no la toca", bot.atajo_lista_pedidos("u", "¿quito el 1?") is None)
bot.texto_lista_pedidos("u"); r = bot.atajo_lista_pedidos("u", "ya pedí todo")
chk("«ya pedí todo» marca todos los pendientes", r and not bot.grupos_faltantes()[0], r.replace("\n", " | ") if r else "None")
bot.LISTA_PEDIDOS["u"]["ts"] -= 3600
chk("pasada media hora sin ver la lista, «quita el 1» ya no hace nada", bot.atajo_lista_pedidos("u", "quita el 1") is None)
base_limpia()
bot.anotar_faltante("x", None, None, 1, 4242, "sin_stock"); bot.anotar_faltante("x", None, None, 1, 4242, "sin_stock")
c = sqlite3.connect(TMP); c.execute("UPDATE demanda SET fecha=?", ((bot.ahora_utc() - bot.timedelta(minutes=30)).isoformat(),)); c.commit(); c.close()
bot.anotar_faltante("x", None, None, 1, 4242, "sin_stock")
chk("cerrar por producto: llegó stock de ese producto → cierra sus 2 pedidos", bot.cerrar_faltantes_producto(4242) == 2 and not bot.grupos_faltantes()[0])
chk("cerrar de nuevo no hace nada", bot.cerrar_faltantes_producto(4242) == 0)

print("\n##### B. (omitido en la copia pública: usaba una copia de la base real)")

print("\n##### C. Con el MODELO real")
def conv(mensajes, n=3, antes=None):
    out = []
    for _ in range(n):
        base_limpia()
        if antes: antes()
        hist = []; paso = []
        for m in mensajes:
            r, hist = bot.procesar_con_openai("u", m, hist); paso.append(r)
        out.append((paso, filas()))
    return out
def prueba(titulo, mensajes, cond, n=3, antes=None):
    o = conv(mensajes, n, antes); ok = sum(1 for p, f in o if cond(p, f)); chk(titulo, ok == n, f"{ok}/{n}")
    return o
OFRECE = "¿Lo anoto en la lista de pedidos"
o = prueba("buscar algo que NO existe → «No encontré… ¿Lo anoto?»; 'sí' → anotado y queda en la lista", ["tienes manguera de calefacción para el Tesla Model 3", "sí"],
           lambda p, f: "No es exactamente lo que buscas" in p[0] and OFRECE in p[0] and "Anotado en la lista de pedidos" in p[1] and len(f) == 1 and f[0][2] == "pendiente" and f[0][4] == "no_existe")
print("      " + o[0][0][0].replace("\n", "\n      ")); print("      " + o[0][0][1].replace("\n", "\n      ")); print("      DB:", o[0][1])
prueba("…con 'no' → «Listo, no lo anoto» y NO queda nada", ["tienes manguera de calefacción para el Tesla Model 3", "no"], lambda p, f: OFRECE in p[0] and "no lo anoto" in p[1] and not f)
prueba("…si en vez de contestar escribe OTRA cosa, la oferta se olvida (y un 'sí' después no anota nada)", ["tienes manguera de calefacción para el Tesla Model 3", "se vendió la 5490", "no", "sí"],
       lambda p, f: OFRECE in p[0] and not f and "Anotado" not in p[3])
o = prueba("una variante concreta SIN stock (corrugada 1 × 14) → «Está sin stock. ¿Lo anoto?» → 'sí' → queda LIGADA al producto de Odoo", ["tienes corrugada de 1 por 14", "sí"],
           lambda p, f: "Está sin stock" in p[0] and OFRECE in p[0] and "Anotado" in p[1] and len(f) == 1 and f[0][3] and f[0][4] == "sin_stock")
print("      DB:", o[0][1])
prueba("algo que no existe de ninguna forma (una sola palabra) → «No encontré» + oferta", ["tienes la zzqxwv"], lambda p, f: "No encontré" in p[0] and OFRECE in p[0])
prueba("producto CON stock (la 5490) → no ofrece nada", ["tienes la 5490"], lambda p, f: OFRECE not in p[0] and "[5490]" in p[0])
prueba("varios resultados (radiador corsa) → no ofrece nada", ["tienes radiador corsa"], lambda p, f: OFRECE not in p[0])
base_limpia(); mem["u"] = {"tipo": "venta", "ventas": [{"product_id": 1, "product_name": "x", "cantidad": 1, "stock_actual": 5}]}
chk("con una VENTA por confirmar NO se ofrece anotar (el 'sí' sería de la venta)", bot.ofrecer_faltante("u", "algo", None, None, None, "no_existe") == "" and "u" not in bot.OFERTA_FALTANTE)
chk("…y sin pendientes ejecutables SÍ se ofrece", (mem.pop("u"), bot.ofrecer_faltante("u", "algo", None, None, None, "no_existe"))[1].startswith("\n\n📋"))
mem["u"] = {"tipo": "venta", "ventas": []}; bot.OFERTA_FALTANTE["u"] = {"nombre": "algo", "categoria": None, "codigo": None, "product_id": None, "motivo": "", "ts": time.time()}
chk("si de pronto hay una venta por confirmar, el 'sí' NO anota (lo toma la venta)", bot.atajo_faltante("u", "sí") is None and not filas())
base_limpia()
prueba("explícito: «vinieron por … y no había» → se anota directo, sin preguntar", ["vinieron por una manguera de calefacción del Tesla y no había"],
       lambda p, f: "Anotado en la lista de pedidos" in p[0] and OFRECE not in p[0] and len(f) == 1 and f[0][4] == "explicito")
prueba("explícito: «anota que pidieron 2 radiadores para Tesla» → directo, 2 unidades", ["anota que pidieron 2 radiadores para Tesla"], lambda p, f: "Anotado" in p[0] and len(f) == 1)
prueba("explícito dos veces seguidas no duplica", ["vinieron por una manguera de calefacción del Tesla y no había", "vinieron por una manguera de calefacción del Tesla y no había"], lambda p, f: len(f) == 1)
def con_lista():
    bot.anotar_faltante("manguera calefacción Tesla", "mangueras para vehiculo", None, 1, None, "no_existe"); bot.anotar_faltante("radiador Tesla", "radiadores", None, 2, None, "explicito")
    c = sqlite3.connect(TMP); c.execute("UPDATE demanda SET fecha=?", ((bot.ahora_utc() - bot.timedelta(hours=30)).isoformat(),)); c.commit(); c.close()
    bot.anotar_faltante("manguera calefacción Tesla", "mangueras para vehiculo", None, 1, None, "no_existe")
o = prueba("«qué me falta pedir?» → la lista numerada (lo más pedido primero)", ["qué me falta pedir?"], lambda p, f: "Lista de pedidos" in p[0] and "1. manguera calefacción Tesla" in p[0].replace("mangueras para vehiculo ", "") or ("1. " in p[0] and "2 pedidos" in p[0] and "ayer" in p[0]), antes=con_lista)
print("      " + o[0][0][0].replace("\n", "\n      "))
prueba("…y «ya pedí el 1» lo pasa a «ya pedidos» (sin modelo)", ["qué me falta pedir?", "ya pedí el 1"], lambda p, f: "Marcados como pedidos" in p[1] and any(x[2] == "pedido" for x in f), antes=con_lista)
prueba("…y «quita el 2» lo saca", ["qué me falta pedir?", "quita el 2"], lambda p, f: "Quité de la lista" in p[1] and any(x[2] == "atendido" for x in f), antes=con_lista)
prueba("«demanda perdida de la semana» sigue funcionando (muestra la lista)", ["demanda perdida de la semana"], lambda p, f: "Lista de pedidos" in p[0], antes=con_lista)
prueba("lista vacía → lo dice", ["qué me falta pedir?"], lambda p, f: "vacía" in p[0])

print("\n##### D. Cierre automático al registrar la llegada")
base_limpia()
pid = bot.odoo_search_read("product.product", [["default_code", "=", "5490"]], ["id"], limit=1)[0]["id"]
bot.anotar_faltante("[5490] test", None, "5490", 1, pid, "sin_stock")
pend = {"tipo": "actualizacion_batch", "actualizaciones": [{"product_id": pid, "product_name": "[5490] test", "cambios": {"agregar_stock": 3}}]}
msg = bot.ejecutar_actualizacion("u", pend)
chk("«llegaron 3» confirmado → «Estaba en la lista de pedidos (1 pedido): ya lo quité» y el pedido queda atendido", "Estaba en la lista de pedidos (1 pedido)" in msg and filas()[0][2] == "atendido", msg.replace("\n", " | ")[:200])
bot.anotar_faltante("[5490] test", None, "5490", 1, pid, "sin_stock")
msg = bot.ejecutar_actualizacion("u", {"tipo": "actualizacion_batch", "actualizaciones": [{"product_id": pid, "product_name": "[5490] test", "cambios": {"nuevo_precio": 1}}]})
chk("un cambio de PRECIO no cierra el pedido", "lista de pedidos" not in msg and filas()[-1][2] == "pendiente")
txt = bot.texto_lista_pedidos("u")
chk("si el producto ya tiene stock en Odoo, al pedir la lista se quita solo y lo avisa", "Ya hay stock" in txt and not bot.grupos_faltantes()[0], txt.replace("\n", " | ")[:200])

print("\n##### E. La ayuda y el resto")
chk("la ayuda cabe en WhatsApp (4096) y menciona la lista", len(bot.TEXTO_AYUDA.replace("__CATEGORIAS__", ", ".join(bot.CATEGORIAS))) <= 4096 and "Lista de pedidos" in bot.TEXTO_AYUDA)
malos = [t for t, c in R if not c]
print(f"\n{len(R) - len(malos)}/{len(R)} comprobaciones OK" + ("" if not malos else "  ← FALLARON: " + "; ".join(malos)))
sys.exit(1 if malos else 0)
