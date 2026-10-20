#!/bin/bash
# Copia de PRUEBA de tu Odoo: restaura el respaldo diario más reciente en una base temporal ('radibombas_prueba')
# y levanta un Odoo temporal en el puerto 8070 (solo local). Sirve para probar cambios en cómo el bot registra ventas,
# entregas y devoluciones SIN tocar la tienda. Producción (puerto 8069, base 'Radibombas') no se toca.
#
#   ./copia_odoo.sh arrancar   → crea la copia y levanta el Odoo temporal (tarda ~30 s; usa ~120 MB de RAM)
#   ./copia_odoo.sh parar      → apaga el Odoo temporal y BORRA la copia y sus archivos
#
# Luego:  python3 probar_entrega_en_copia.py   (apunta SOLO a la copia y aborta si no lo está)
# Hacerlo fuera del horario de atención si se puede (el servidor tiene poca RAM).
set -euo pipefail
cd /
DB="radibombas_prueba"; PUERTO=8070; DATOS="/tmp/odoo_prueba_data"; LOG="/tmp/odoo_prueba.log"

pid_del_temporal() { ss -ltnp 2>/dev/null | grep ":$PUERTO " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true; }

case "${1:-}" in
  arrancar)
    [ -n "$(pid_del_temporal)" ] && { echo "Ya hay algo escuchando en el puerto $PUERTO. Usa 'parar' primero."; exit 1; }
    DUMP="$(ls -t ${RADIBOT_BACKUP_DIR:-/var/backups/radibot/daily}/*.dump | head -1)"
    echo "Respaldo usado: $DUMP"
    cp "$DUMP" /tmp/prueba_restaura.dump; chmod 644 /tmp/prueba_restaura.dump
    su postgres -c "dropdb --if-exists $DB"
    su postgres -c "createdb -O odoo $DB"
    su postgres -c "pg_restore --no-owner --role=odoo -d $DB /tmp/prueba_restaura.dump" || true
    rm -f /tmp/prueba_restaura.dump
    mkdir -p "$DATOS"; chown -R odoo:odoo "$DATOS"; chmod 777 "$DATOS"
    setsid nohup nice -n 10 runuser -u odoo -- /opt/odoo/venv/bin/python3 /opt/odoo/odoo/odoo-bin -c /etc/odoo.conf \
        -d "$DB" --http-port=$PUERTO --data-dir="$DATOS" --logfile="$LOG" --max-cron-threads=0 --workers=0 \
        --db-filter="^$DB\$" --no-database-list > /tmp/odoo_prueba.out 2>&1 &
    for i in $(seq 1 40); do
      sleep 3
      python3 -c "import xmlrpc.client; print('Odoo temporal listo, versión', xmlrpc.client.ServerProxy('http://127.0.0.1:$PUERTO/xmlrpc/2/common').version()['server_version'])" 2>/dev/null && exit 0
    done
    echo "El Odoo temporal no respondió; revisa $LOG"; exit 1 ;;
  parar)
    PID="$(pid_del_temporal)"
    # (no usar 'pkill -f' con el nombre de la base: coincide con este mismo comando y mata la terminal)
    [ -n "$PID" ] && kill "$PID" && sleep 3 && echo "Odoo temporal apagado (PID $PID)"
    su postgres -c "psql -At -c \"select count(pg_terminate_backend(pid)) from pg_stat_activity where datname = '$DB'\"" >/dev/null 2>&1 || true
    sleep 1
    su postgres -c "dropdb --if-exists $DB" && echo "Base de prueba eliminada"
    rm -rf "$DATOS" "$LOG" /tmp/odoo_prueba.out /tmp/prueba_restaura.dump
    echo "Bases que quedan: $(su postgres -c "psql -At -c \"select string_agg(datname, ', ') from pg_database where datname not like 'template%' and datname <> 'postgres'\"")" ;;
  *) sed -n 2,11p "$0"; exit 1 ;;
esac
