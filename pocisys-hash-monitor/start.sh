#!/bin/sh
set -eu

PACKAGES="/tmp/pocisys-packages"
rm -rf "$PACKAGES"
mkdir -p "$PACKAGES"

python -m pip install \
  --no-index \
  --no-cache-dir \
  --no-compile \
  --find-links /app/wheels \
  --target "$PACKAGES" \
  -r /app/requirements.txt

export PYTHONPATH="$PACKAGES:/app"
exec python /app/main.py
