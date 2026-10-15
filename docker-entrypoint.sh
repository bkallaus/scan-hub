#!/bin/sh
set -e

# Optional: re-enable extra SANE backends, e.g. SANE_EXTRA_BACKENDS="epsonds escl"
if [ -n "$SANE_EXTRA_BACKENDS" ]; then
  for b in $SANE_EXTRA_BACKENDS; do
    grep -qx "$b" /etc/sane.d/dll.conf || echo "$b" >> /etc/sane.d/dll.conf
  done
fi

mkdir -p "$DATA_DIR/scans"
exec "$@"
