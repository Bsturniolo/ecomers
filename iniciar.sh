#!/usr/bin/env bash
# Arranca el panel en esta compu (Linux o Mac). Doble clic o: ./iniciar.sh
# La primera vez instala lo necesario y crea la clave. Los datos quedan en la carpeta "datos".
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r conector/requirements.txt
mkdir -p datos
[ -f datos/clave.txt ] || python3 -c "import secrets; print(secrets.token_hex(12))" > datos/clave.txt
[ -f datos/config.env ] || cp config.ejemplo.env datos/config.env
set -a; . datos/config.env; set +a
export ADMIN_TOKEN="$(cat datos/clave.txt)" CONECTOR_DB="$PWD/datos/conector.db"
echo ""
echo "  Panel: http://localhost:8000/panel"
echo "  Clave: $ADMIN_TOKEN"
echo "  (para cerrar: Ctrl+C)"
echo ""
(sleep 3; xdg-open http://localhost:8000/panel >/dev/null 2>&1 || open http://localhost:8000/panel >/dev/null 2>&1 || true) &
cd conector
exec python -m uvicorn app:app --host 127.0.0.1 --port 8000
