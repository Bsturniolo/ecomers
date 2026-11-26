#!/usr/bin/env sh
# Arma el sitio de GitHub Pages en la carpeta que se le pase (por defecto _site).
# El panel se copia de conector/panel.html y se le suma demo.js, así la demo siempre es igual al panel real.
set -e
cd "$(dirname "$0")/.."
SALIDA="${1:-_site}"
rm -rf "$SALIDA"
mkdir -p "$SALIDA"
cp -R pages/. "$SALIDA/"
rm -f "$SALIDA/armar.sh"
sed 's|^<head>$|<head>\n<script src="demo.js"></script>|' conector/panel.html > "$SALIDA/panel/index.html"
grep -q 'src="demo.js"' "$SALIDA/panel/index.html" || { echo "No se pudo sumar demo.js al panel" >&2; exit 1; }
touch "$SALIDA/.nojekyll"
echo "Sitio armado en $SALIDA"
