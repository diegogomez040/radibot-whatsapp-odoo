"""Revisa (SOLO LECTURA) las últimas ventas del bot en Odoo: la orden, su entrega, el stock y las reservas.
Uso:  python3 pruebas/verificar_ultima_venta.py [cuántas]     (por defecto 1)
Sirve para comprobar, con una venta real, que el modo de entrega funciona (MODO_ENTREGA=odoo)."""
import os, sys, json
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); os.chdir(ROOT); sys.path.insert(0, ROOT)
import logging; logging.disable(logging.CRITICAL)
import bot

n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
ventas = json.load(open("data/sales_log.json"))[-n:]
print(f"Modo de entrega configurado en el bot: {bot.MODO_ENTREGA}\n")
for v in ventas:
    oid, pid = v["orden_id"], v["product_id"]
    o = bot.odoo_search_read("sale.order", [["id", "=", oid]], ["name", "state", "amount_total", "order_line"], limit=1)
    if not o:
        print(f"Orden {oid}: no existe en Odoo"); continue
    o = o[0]
    lin = bot.odoo_search_read("sale.order.line", [["id", "in", o["order_line"]]], ["product_uom_qty", "qty_delivered"])
    entregas = bot.odoo_search_read("stock.picking", [["sale_id", "=", oid]], ["name", "state", "picking_type_code"])
    devs = bot.odoo_search_read("stock.picking", [["return_id", "in", [e["id"] for e in entregas]]], ["name", "state"]) if entregas else []
    hechas = [e for e in entregas if e["state"] == "done" and e["picking_type_code"] == "outgoing"]
    canceladas = [e for e in entregas if e["state"] == "cancel"]
    abiertas = [e for e in entregas if e["state"] not in ("done", "cancel")]
    ahora = bot.get_stock_actual(pid)
    esperado = v["stock_anterior"] - v["cantidad"] + v.get("devuelto", 0)
    print(f"Orden {o['name']} — {bot.fecha_colombia(v['timestamp'])} — {v['product_name'][:60]} ×{v['cantidad']}")
    print(f"   orden: estado={o['state']} | total=${o['amount_total']:,.0f} | pedida={[int(x['product_uom_qty']) for x in lin]} entregada={[int(x['qty_delivered']) for x in lin]}")
    print(f"   entregas: {[(e['name'], e['state']) for e in entregas]} | devoluciones: {[(d['name'], d['state']) for d in devs]}")
    if abiertas:
        veredicto = "⚠️ HAY UNA ENTREGA ABIERTA (reservaría unidades de más)"
    elif hechas:
        veredicto = "✅ forma correcta: la entrega está HECHA y Odoo restó el stock"
    elif canceladas:
        veredicto = "ℹ️ modo anterior: entrega cancelada y stock restado por el bot"
    else:
        veredicto = "⚠️ la orden no tiene entregas"
    print(f"   {veredicto}")
    print(f"   stock: antes de la venta={v['stock_anterior']} | esperado hoy={esperado} | real hoy={ahora:.0f}" + ("" if abs(ahora - esperado) < 1e-6 else "   (distinto: puede ser por otras ventas o ingresos de ese producto después)"))
q = bot.odoo_search_read("stock.quant", [["location_id.usage", "=", "internal"]], ["reserved_quantity"])
abiertas_total = bot.odoo_search_read("stock.picking", [["sale_id.partner_id", "=", bot.CLIENTE_MOSTRADOR_ID], ["state", "in", ["assigned", "confirmed"]]], ["name"])
print(f"\nGlobal: entregas abiertas del Cliente Mostrador = {len(abiertas_total)} | unidades reservadas en total = {sum(x['reserved_quantity'] for x in q):.0f}   (ambas deben ser 0)")
