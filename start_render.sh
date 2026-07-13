#!/bin/bash
# Script de arranque para Render — inicializa BD si no existe
DB_PATH="${CFO_DB_PATH:-/data/cfo.db}"

if [ ! -f "$DB_PATH" ]; then
  echo "Inicializando BD desde seed..."
  sqlite3 "$DB_PATH" < seed.sql
  echo "BD lista en $DB_PATH"
fi

exec gunicorn app:app --bind 0.0.0.0:$PORT
