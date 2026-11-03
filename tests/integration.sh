#!/usr/bin/env bash
# Round trip against a real server: load a fixture with every kind of object,
# back it up, drop everything, restore, and compare. Then a partial restore
# (two tables under a new name, one user).
#
#   MBM_DB_USER=root tests/integration.sh             # local socket
#   MBM_DB_HOST=127.0.0.1 MBM_DB_USER=root MBM_DB_PASSWORD=pw tests/integration.sh
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

ARGS=(-u "${MBM_DB_USER:-root}")
[ -n "${MBM_DB_HOST:-}" ] && ARGS+=(-H "$MBM_DB_HOST")
[ -n "${MBM_DB_PORT:-}" ] && ARGS+=(-P "$MBM_DB_PORT")
SQL=(mariadb -u "${MBM_DB_USER:-root}")
[ -n "${MBM_DB_HOST:-}" ] && SQL+=(-h "$MBM_DB_HOST")
[ -n "${MBM_DB_PORT:-}" ] && SQL+=(-P "$MBM_DB_PORT")
[ -n "${MBM_DB_PASSWORD:-}" ] && SQL+=("-p$MBM_DB_PASSWORD")
export MBM_DB_PASSWORD="${MBM_DB_PASSWORD:-}"

snapshot() {
  "${SQL[@]}" -N -e "CHECKSUM TABLE shop.customers, shop.orders, shop.audit, hr.staff;
    SHOW CREATE TABLE shop.orders; SHOW CREATE VIEW shop.v_big;
    SHOW GRANTS FOR 'app'@'%'; SHOW GRANTS FOR 'report'@'localhost'; SHOW GRANTS FOR readonly;
    SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='shop' ORDER BY 1;
    SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA='shop';
    SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA='shop';" | sed 's/ AUTO_INCREMENT=[0-9]*//'
}

echo "== load fixture"
"${SQL[@]}" -e "DROP DATABASE IF EXISTS shop_copy"
"${SQL[@]}" < tests/fixture.sql
snapshot > "$OUT/before.txt"

echo "== backup"
FILE=$(python3 -m mbm.cli backup "${ARGS[@]}" -d shop -d hr -o "$OUT" --label it 2>"$OUT/backup.log")
python3 -m mbm.cli verify "$FILE"

echo "== drop everything and restore"
"${SQL[@]}" -e "DROP DATABASE shop; DROP DATABASE hr; DROP USER 'app'@'%', 'report'@'localhost'; DROP ROLE readonly;"
python3 -m mbm.cli restore "$FILE" --all "${ARGS[@]}" -y 2>"$OUT/restore.log" || { cat "$OUT/restore.log"; exit 1; }
snapshot > "$OUT/after.txt"
diff "$OUT/before.txt" "$OUT/after.txt" && echo "   identical after restore"

echo "== the restored password still works"
mariadb ${MBM_DB_HOST:+-h "$MBM_DB_HOST"} ${MBM_DB_PORT:+-P "$MBM_DB_PORT"} -u app -p's3cret!' -N -e "SELECT COUNT(*) FROM shop.customers" >/dev/null \
  && echo "   app@% signs in" || { [ -z "${MBM_DB_HOST:-}" ] && echo "   (skipped: app@% is not reachable over the local socket)"; }

echo "== partial restore under a new name"
python3 -m mbm.cli restore "$FILE" "${ARGS[@]}" --db shop.orders --db shop.customers --rename shop=shop_copy --schema-only -y 2>"$OUT/partial.log"
got=$("${SQL[@]}" -N -e "SELECT GROUP_CONCAT(TABLE_NAME ORDER BY TABLE_NAME) FROM information_schema.TABLES WHERE TABLE_SCHEMA='shop_copy'; SELECT COUNT(*) FROM shop_copy.orders")
[ "$got" = "$(printf 'customers,orders\n0')" ] || { echo "unexpected: $got"; cat "$OUT/partial.log"; exit 1; }
echo "   shop_copy has customers and orders, structure only"

echo "== restore SQL can be written without a server"
python3 -m mbm.cli restore "$FILE" --db hr --sql-out "$OUT/hr.sql" 2>/dev/null
grep -q 'CREATE TABLE `staff`' "$OUT/hr.sql" && echo "   hr.sql written"

"${SQL[@]}" -e "DROP DATABASE shop_copy"
echo "integration ok"
