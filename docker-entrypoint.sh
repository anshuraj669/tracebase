#!/bin/sh
# Make /data private to the tracebase user, then run Tracebase without root.
set -e
umask 077
DATA="${TRACEBASE_DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ] && [ "${TRACEBASE_RUN_AS_ROOT:-0}" != "1" ]; then
  mkdir -p "$DATA"
  # volumes created by older (root) versions: hand them over once
  if [ "$(stat -c %u "$DATA")" != "10001" ] || find "$DATA" ! -user 10001 -print -quit | grep -q .; then
    chown -R 10001:10001 "$DATA"
  fi
  chmod 700 "$DATA"
  exec setpriv --reuid=10001 --regid=10001 --init-groups "$@"
fi
exec "$@"
