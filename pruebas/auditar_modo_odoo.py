"""Auditoría de SOLO LECTURA del modo MODO_ENTREGA=odoo: ¿todo lo que ha hecho el bot desde la activación quedó bien en Odoo?
Uso:  python3 pruebas/auditar_modo_odoo.py [desde]      (desde = '2026-10-07 15:33:00' en UTC; por defecto, la activación)
Revisa: órdenes y entregas creadas desde entonces, devoluciones, que el stock real = stock antes + movimientos hechos, entregas abiertas /
reservas / stock negativo en TODO el inventario, asientos contables inesperados, y que el registro del bot coincida con Odoo."""
import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
import logging; logging.disable(logging.CRITICAL)
import bot

DESDE = sys.argv[1] if len(sys.argv) > 1 else "2026-10-07 15:33:00"   # 10:33 Colombia = 15:33 UTC (activación del modo odoo)
problemas, avisos = [], []
def ok(t): print(f"  ✅ {t}")
def mal(t): problemas.append(t); print(f"  ❌ {t}")
def aviso(t): avisos.append(t); print(f"  ⚠️  {t}")
sr = bot.odoo_search_read

print(f"Modo del bot: {bot.MODO_ENTREGA} | revisando desde {DESDE} UTC\n")

# ── 1. Órdenes del bot desde la activación ──────────────────────────────────────────────
print("1. Órdenes creadas desde la activación")
ordenes = sr("sale.order", [["create_date", ">=", DESDE], ["partner_id", "=", bot.CLIENTE_MOSTRADOR_ID]],
             ["name", "state", "amount_total", "order_line", "picking_ids", "invoice_status", "create_date"], order="id asc")
print(f"   {len(ordenes)} orden(es): " + ", ".join(f"{o['name']} ({o['state']})" for o in ordenes))
productos_tocados = set()
for o in ordenes:
    lin = sr("sale.order.line", [["id", "in", o["order_line"]]], ["product_id", "product_uom_qty", "qty_delivered", "price_unit"])
    picks = sr("stock.picking", [["id", "in", o["picking_ids"]]], ["name", "state", "picking_type_code", "return_id", "date_done"], order="id asc")
    salidas = [p for p in picks if p["picking_type_code"] == "outgoing"]
    entradas = [p for p in picks if p["picking_type_code"] == "incoming"]
    abiertas = [p for p in picks if p["state"] not in ("done", "cancel")]
    for l in lin:
        productos_tocados.add(l["product_id"][0])
    etiqueta = f"{o['name']} [{o['state']}] ${o['amount_total']:,.0f} pedida={[int(l['product_uom_qty']) for l in lin]} entregada={[int(l['qty_delivered']) for l in lin]}"
    print(f"   • {etiqueta}\n       entregas: {[(p['name'], p['state']) for p in salidas]} | devoluciones: {[(p['name'], p['state']) for p in entradas]}")
    if abiertas:
        mal(f"{o['name']}: tiene operaciones ABIERTAS {[(p['name'], p['state']) for p in abiertas]}")
    if o["state"] in ("sale", "done"):
        if not any(p["state"] == "done" for p in salidas):
            mal(f"{o['name']}: confirmada pero SIN entrega hecha")
        neto = sum(l["qty_delivered"] for l in lin)
        pedida = sum(l["product_uom_qty"] for l in lin)
        if neto != pedida:
            mal(f"{o['name']}: pedida {pedida:.0f} pero entregada {neto:.0f}")
    if o["state"] == "cancel" and salidas and all(p["state"] == "done" for p in salidas) and not entradas:
        mal(f"{o['name']}: CANCELADA con entrega hecha y SIN devolución (el stock no volvió)")
    if o["state"] == "cancel" and entradas:
        sal_q = sum(m["quantity"] for m in sr("stock.move", [["picking_id", "in", [p["id"] for p in salidas]], ["state", "=", "done"]], ["quantity"]))
        ent_q = sum(m["quantity"] for m in sr("stock.move", [["picking_id", "in", [p["id"] for p in entradas]], ["state", "=", "done"]], ["quantity"]))
        (ok if sal_q == ent_q else mal)(f"{o['name']}: cancelada → salió {sal_q:.0f} y volvió {ent_q:.0f}")
if not ordenes:
    aviso("no hay órdenes del bot desde la activación")

# ── 2. Stock de cada venta ───────────────────────────────────────────────────────────────
print("\n2. Por cada venta: el stock justo DESPUÉS de la entrega = stock antes - cantidad (se calcula hacia atrás con los movimientos posteriores)")
log = json.load(open("data/sales_log.json"))
usuarios_orden = {o["id"] for o in ordenes}
usos = {}
def uso(loc_id):
    if loc_id not in usos:
        usos[loc_id] = sr("stock.location", [["id", "=", loc_id]], ["usage"])[0]["usage"]
    return usos[loc_id]
def firmado(m):
    return (m["quantity"] if uso(m["location_dest_id"][0]) == "internal" else 0) - (m["quantity"] if uso(m["location_id"][0]) == "internal" else 0)
for v in [x for x in log if x["orden_id"] in usuarios_orden]:
    entregas = sr("stock.picking", [["sale_id", "=", v["orden_id"]], ["picking_type_code", "=", "outgoing"], ["state", "=", "done"]], ["id", "name"])
    if not entregas:
        aviso(f"orden {v['orden_id']}: sin entrega hecha, no se puede comprobar el stock"); continue
    mov_entrega = sr("stock.move", [["picking_id", "in", [e["id"] for e in entregas]], ["product_id", "=", v["product_id"]], ["state", "=", "done"]], ["id", "quantity"])
    ultimo = max(m["id"] for m in mov_entrega)
    despues = sr("stock.move", [["product_id", "=", v["product_id"]], ["state", "=", "done"], ["id", ">", ultimo]], ["quantity", "location_id", "location_dest_id", "reference"])
    real = bot.get_stock_actual(v["product_id"])
    justo_despues = real - sum(firmado(m) for m in despues)
    esperado = v["stock_anterior"] - v["cantidad"]
    mov_ok = sum(m["quantity"] for m in mov_entrega) == v["cantidad"]
    (ok if abs(justo_despues - esperado) < 1e-6 and mov_ok else mal)(
        f"{entregas[0]['name']} {v['product_name'][:42]}: antes={v['stock_anterior']} vendió {v['cantidad']} → debía quedar {esperado}, quedó {justo_despues:.0f}; "
        f"el movimiento de la entrega es de {sum(m['quantity'] for m in mov_entrega):.0f}; movimientos posteriores: {[(m['reference'][:20], firmado(m)) for m in despues] or 'ninguno'}")

# ── 3. Estado global ─────────────────────────────────────────────────────────────────────
print("\n3. Estado global del inventario")
abiertas = sr("stock.picking", [["state", "in", ["draft", "waiting", "confirmed", "assigned"]]], ["name", "state", "picking_type_code"])
(ok if not abiertas else mal)(f"operaciones de inventario abiertas (todas): {len(abiertas)}" + (f" → {[(a['name'], a['state']) for a in abiertas[:8]]}" if abiertas else ""))
quants = sr("stock.quant", [["location_id.usage", "=", "internal"]], ["product_id", "quantity", "reserved_quantity"])
reservado = sum(q["reserved_quantity"] for q in quants)
(ok if reservado == 0 else mal)(f"unidades reservadas en total: {reservado:.0f}")
neg = [q for q in quants if q["quantity"] < 0]
(ok if not neg else aviso)(f"quants con stock negativo: {len(neg)}" + (f" → {[(q['product_id'][1][:30], q['quantity']) for q in neg[:5]]}" if neg else ""))
total = sum(q["quantity"] for q in quants)
print(f"   (stock total en bodega: {total:.0f} unidades; el 2026-10-06 eran 2487 antes de ventas/devoluciones del día)")

# ── 4. Contabilidad y facturación ────────────────────────────────────────────────────────
print("\n4. Contabilidad y facturación (no debe aparecer nada nuevo por las entregas)")
asientos = sr("account.move", [["create_date", ">=", DESDE]], ["name", "move_type", "state", "ref"])
(ok if not asientos else aviso)(f"asientos/facturas creados desde la activación: {len(asientos)}" + (f" → {[(a['name'], a['move_type']) for a in asientos[:6]]}" if asientos else ""))
val = sr("stock.valuation.layer", [["create_date", ">=", DESDE]], ["id"], limit=5) if sr("ir.model", [["model", "=", "stock.valuation.layer"]], ["id"], limit=1) else []
print(f"   capas de valoración nuevas: {len(val)} (la valoración de esta tienda es periódica/estándar)")
fact = {}
for o in sr("sale.order", [["partner_id", "=", bot.CLIENTE_MOSTRADOR_ID], ["state", "=", "sale"], ["create_date", ">=", "2026-09-01 00:00:00"]], ["invoice_status", "create_date"]):
    k = ("después" if o["create_date"] >= DESDE else "antes") + " de la activación"
    fact.setdefault(k, {}).setdefault(o["invoice_status"], 0)
    fact[k][o["invoice_status"]] += 1
print(f"   estado de facturación de las órdenes confirmadas (sep-oct): {fact}  → debe ser igual de uno y otro lado (la facturación real se hace en la DIAN)")

# ── 5. Registro del bot vs Odoo ──────────────────────────────────────────────────────────
print("\n5. Registro del bot (sales_log.json) vs Odoo")
desde_log = [v for v in log if v["orden_id"] in usuarios_orden]
nombres = {o["id"]: o for o in ordenes}
for v in desde_log:
    o = nombres[v["orden_id"]]
    esperado_cancel = v.get("deshecha", False)
    coincide = (o["state"] == "cancel") == esperado_cancel
    (ok if coincide else mal)(f"{o['name']}: bot dice {'deshecha' if esperado_cancel else 'viva'}, devuelto={v.get('devuelto', 0)}/{v['cantidad']} — Odoo dice {o['state']}")
sin_log = [o["name"] for o in ordenes if o["id"] not in {v["orden_id"] for v in desde_log}]
(ok if not sin_log else mal)(f"órdenes de Odoo sin registro en el bot: {sin_log or 'ninguna'}")

# ── 6. Resumen de ventas del bot ─────────────────────────────────────────────────────────
print("\n6. «Resumen de hoy» del bot")
print("   " + bot.resumen_ventas_periodo("hoy").replace("\n", "\n   "))
vivas = [o for o in ordenes if o["state"] == "sale"]
print(f"   (órdenes vivas del bot desde la activación: {len(vivas)} por ${sum(o['amount_total'] for o in vivas):,.0f})")

print("\n" + ("RESULTADO: todo bien ✅" if not problemas else f"RESULTADO: {len(problemas)} problema(s) ❌") + (f" | {len(avisos)} aviso(s) ⚠️" if avisos else ""))
