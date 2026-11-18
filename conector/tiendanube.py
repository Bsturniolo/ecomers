"""Todo lo que habla con Tiendanube: instalación (OAuth), firma de los avisos y la API.

Documentación oficial: https://tiendanube.github.io/api-documentation/
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx

URL_API = "https://api.tiendanube.com/2025-03"
URL_TOKEN = "https://www.tiendanube.com/apps/authorize/token"

# Avisos que pedimos a Tiendanube al instalar la app.
# order/updated y order/fulfilled sirven para tener al día el estado del envío de cada venta.
EVENTOS = ("order/paid", "order/cancelled", "order/updated", "order/fulfilled", "app/uninstalled")


def url_instalar(app_id: str, estado: str) -> str:
    """Página de Tiendanube donde la clienta acepta instalar nuestra app."""
    return f"https://www.tiendanube.com/apps/{app_id}/authorize?" + urlencode({"state": estado})


def firma_valida(secreto_app: str, cuerpo: bytes, firma: str) -> bool:
    """Tiendanube firma cada aviso: HMAC-SHA256 (hex) del cuerpo con el secreto de la app.

    Viene en el encabezado x-linkedstore-hmac-sha256.
    """
    if not secreto_app or not firma:
        return False
    esperada = hmac.new(secreto_app.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, firma.strip().lower())


class ErrorTiendanube(Exception):
    def __init__(self, mensaje: str, status: int | None = None):
        super().__init__(mensaje)
        self.status = status


def canjear_codigo(app_id: str, secreto_app: str, codigo: str, http: httpx.Client | None = None) -> dict:
    """Cambia el código que manda Tiendanube al instalar por la llave de acceso (no vence)."""
    cliente = http or httpx.Client(timeout=20)
    try:
        r = cliente.post(URL_TOKEN, json={"client_id": app_id, "client_secret": secreto_app,
                                          "grant_type": "authorization_code", "code": codigo})
    except httpx.HTTPError as e:
        raise ErrorTiendanube(f"No se pudo conectar con Tiendanube (¿hay internet?): {e}")
    datos = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "access_token" not in datos:
        raise ErrorTiendanube(f"Tiendanube no aceptó el código ({r.status_code}): {datos.get('error_description') or r.text[:200]}", r.status_code)
    return {"token": datos["access_token"], "tienda_id": str(datos.get("user_id") or datos.get("store_id"))}


class API:
    """Cliente mínimo de la API de Tiendanube para una tienda."""

    def __init__(self, tienda_id: str, token: str, contacto: str, http: httpx.Client | None = None,
                 url_api: str = URL_API):
        self.base = f"{url_api.rstrip('/')}/{tienda_id}"
        self.http = http or httpx.Client(timeout=20)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Authentication": f"bearer {token}",  # nombre viejo del encabezado; algunas versiones de la API todavía lo leen
            # Tiendanube exige identificar la app con un contacto; sin esto responde 400.
            "User-Agent": f"Conector Ecomers ({contacto})",
            "Content-Type": "application/json",
        }

    def _pedir(self, metodo: str, ruta: str, **kw) -> httpx.Response:
        for intento in range(4):
            try:
                r = self.http.request(metodo, self.base + ruta, headers=self.headers, **kw)
            except httpx.HTTPError as e:
                raise ErrorTiendanube(f"No se pudo conectar con Tiendanube (¿hay internet?): {e}")
            if r.status_code == 429 and intento < 3:  # límite de pedidos: esperar y reintentar
                time.sleep(min(float(r.headers.get("x-rate-limit-reset", "1000")) / 1000, 5))
                continue
            break
        if r.status_code >= 400:
            raise ErrorTiendanube(f"{metodo} {ruta} respondió {r.status_code}: {r.text[:300]}", r.status_code)
        return r

    def pedido(self, pedido_id: int) -> dict:
        return self._pedir("GET", f"/orders/{int(pedido_id)}").json()

    def pedidos_desde(self, desde_iso: str) -> list[dict]:
        """Todos los pedidos creados o cambiados desde esa fecha (de a 200 por página, los más viejos primero)."""
        todos, pagina = [], 1
        while True:
            try:
                lote = self._pedir("GET", "/orders", params={"updated_at_min": desde_iso, "per_page": 200, "page": pagina}).json()
            except ErrorTiendanube as e:
                if e.status == 404:  # Tiendanube responde 404 cuando no hay (más) resultados
                    break
                raise
            todos.extend(lote)
            if len(lote) < 200 or pagina >= 100:
                break
            pagina += 1
        return sorted(todos, key=lambda p: (str(p.get("updated_at") or ""), int(p.get("id") or 0)))

    def productos(self) -> list[dict]:
        """Todos los productos con sus variantes (de a 200 por página)."""
        todos, pagina = [], 1
        while True:
            try:
                lote = self._pedir("GET", "/products", params={"page": pagina, "per_page": 200}).json()
            except ErrorTiendanube as e:
                if e.status == 404 and pagina > 1:  # Tiendanube responde 404 cuando se pasa de la última página
                    break
                raise
            todos.extend(lote)
            if len(lote) < 200:
                break
            pagina += 1
        return todos

    def poner_stock(self, producto_id: int, variante_id: int, stock: float) -> None:
        """Reemplaza el stock de una variante en Tiendanube."""
        self._pedir("POST", f"/products/{int(producto_id)}/variants/stock",
                    json={"action": "replace", "value": int(stock), "id": int(variante_id)})

    def webhooks(self) -> list[dict]:
        return self._pedir("GET", "/webhooks").json()

    def crear_webhook(self, evento: str, url: str) -> dict:
        return self._pedir("POST", "/webhooks", json={"event": evento, "url": url}).json()

    def borrar_webhook(self, webhook_id: int) -> None:
        self._pedir("DELETE", f"/webhooks/{int(webhook_id)}")

    def registrar_avisos(self, url: str) -> list[str]:
        """Deja pedidos exactamente los avisos de EVENTOS apuntando a url (sin duplicar)."""
        actuales = self.webhooks()
        hechos = []
        for w in actuales:
            if w.get("url") == url and w.get("event") not in EVENTOS:
                self.borrar_webhook(w["id"])
        ya = {w.get("event") for w in actuales if w.get("url") == url}
        for ev in EVENTOS:
            if ev not in ya:
                self.crear_webhook(ev, url)
            hechos.append(ev)
        return hechos
