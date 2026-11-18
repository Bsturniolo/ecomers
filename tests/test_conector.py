"""Pruebas de punta a punta con una Tiendanube falsa (no hace falta tienda ni internet)."""
import hashlib
import hmac
import json
import pathlib
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "conector"))
import app as conector  # noqa: E402

TOKEN = "token-de-prueba-123456"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}
SECRETO = "secreto-de-la-app"
TIENDA = "4242"


class TiendanubeFalsa:
    """Imita la API de Tiendanube: productos, pedidos, stock y webhooks."""

    def __init__(self):
        self.productos = [
            {"id": 10, "name": {"es": "Ventilador de pie"}, "variants": [
                {"id": 101, "sku": "VENT-01", "barcode": "", "price": "45000.00", "stock": 8, "stock_management": True, "values": []}]},
            {"id": 20, "name": {"es": "Funda celular"}, "variants": [
                {"id": 201, "sku": "FUN-NEG", "price": "6000.00", "stock": 30, "values": [{"es": "Negra"}]},
                {"id": 202, "sku": "FUN-ROJ", "price": "6000.00", "stock": 12, "values": [{"es": "Roja"}]}]},
            {"id": 30, "name": {"es": "Sticker"}, "variants": [
                {"id": 301, "sku": "STK", "price": "500.00", "stock": None, "stock_management": False, "values": []}]},
        ]
        self.pedidos = {}
        self.webhooks = []
        self.stock_enviado = []
        self.falla_stock = False
        self.pedidos_hechos = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.pedidos_hechos.append(req)
        ruta = req.url.path
        if req.url.host == "www.tiendanube.com" and ruta == "/apps/authorize/token":
            datos = json.loads(req.content)
            if datos["code"] != "codigo-bueno" or datos["client_secret"] != SECRETO:
                return httpx.Response(400, json={"error_description": "código inválido"})
            return httpx.Response(200, json={"access_token": "llave-123", "token_type": "bearer", "user_id": int(TIENDA)})
        assert req.headers["Authorization"] == "Bearer llave-123"
        assert "User-Agent" in req.headers and "Conector" in req.headers["User-Agent"]
        pre = f"/2025-03/{TIENDA}"
        assert ruta.startswith(pre), ruta
        ruta = ruta[len(pre):]
        if ruta == "/products":
            return httpx.Response(200, json=self.productos)
        if ruta.startswith("/orders/"):
            p = self.pedidos.get(int(ruta.split("/")[-1]))
            return httpx.Response(200, json=p) if p else httpx.Response(404, json={})
        if ruta.endswith("/variants/stock"):
            if self.falla_stock:
                return httpx.Response(500, json={})
            self.stock_enviado.append((int(ruta.split("/")[2]), json.loads(req.content)))
            return httpx.Response(200, json=[])
        if ruta == "/webhooks" and req.method == "GET":
            return httpx.Response(200, json=self.webhooks)
        if ruta == "/webhooks" and req.method == "POST":
            w = {"id": len(self.webhooks) + 1, **json.loads(req.content)}
            self.webhooks.append(w)
            return httpx.Response(201, json=w)
        return httpx.Response(404, json={"ruta": ruta})


@pytest.fixture
def tn():
    return TiendanubeFalsa()


@pytest.fixture
def c(tmp_path, tn):
    a = conector.crear_app(str(tmp_path / "t.db"), admin_token=TOKEN, app_id="999", secreto_app=SECRETO,
                           url_publica="https://tienda.ejemplo.com", contacto="prueba@ejemplo.com",
                           tienda_esperada="", http=httpx.Client(transport=httpx.MockTransport(tn)), tareas_de_fondo=False)
    cl = TestClient(a)
    cl.app_ = a
    return cl


def firmar(cuerpo: bytes, secreto=SECRETO) -> str:
    return hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def avisar(c, evento, recurso_id, secreto=SECRETO, tienda=TIENDA):
    cuerpo = json.dumps({"store_id": int(tienda), "event": evento, "id": recurso_id}).encode()
    return c.post("/tiendanube/aviso", content=cuerpo, headers={"x-linkedstore-hmac-sha256": firmar(cuerpo, secreto),
                                                                "Content-Type": "application/json"})


def conectar(c):
    estado = c.post("/api/instalar", headers=ADMIN).json()["url"].split("state=")[1]
    r = c.get(f"/tiendanube/instalada?code=codigo-bueno&state={estado}")
    assert r.status_code == 200, r.text
    return r


def prod(c, sku):
    return next(p for p in c.get("/api/productos", headers=ADMIN).json() if p["sku"] == sku)


def pedido(id_, items, pago="paid", numero=None):
    total = sum(float(i["price"]) * i["quantity"] for i in items) + 3000
    return {"id": id_, "number": numero or id_ - 1000, "payment_status": pago, "subtotal": total - 3000,
            "shipping_cost_customer": "3000.00", "discount": "0.00", "total": f"{total:.2f}", "gateway_name": "Mercado Pago",
            "paid_at": "2026-11-17T21:00:00-03:00", "customer": {"id": 7, "name": "Ana", "email": "ana@ejemplo.com", "phone": "261"},
            "products": items}


# ---------------------------------------------------------------- seguridad
def test_panel_pide_clave(c):
    assert c.get("/api/estado").status_code == 401
    assert c.get("/api/estado", headers={"Authorization": "Bearer otra-clave-cualquiera"}).status_code == 401
    assert c.get("/api/estado", headers=ADMIN).status_code == 200
    assert c.get("/panel").status_code == 200


def test_aviso_con_firma_mala_se_rechaza(c, tn):
    conectar(c)
    assert avisar(c, "order/paid", 1, secreto="otro").status_code == 401
    assert c.get("/api/avisos", headers=ADMIN).json() == []


def test_aviso_de_otra_tienda_se_rechaza(c):
    conectar(c)
    assert avisar(c, "order/paid", 1, tienda="1111").status_code == 403


def test_instalacion_con_estado_viejo_falla(c):
    c.post("/api/instalar", headers=ADMIN)
    assert c.get("/tiendanube/instalada?code=codigo-bueno&state=cualquiera").status_code == 400


def test_instalacion_en_otra_tienda_se_rechaza(tmp_path, tn):
    a = conector.crear_app(str(tmp_path / "t.db"), admin_token=TOKEN, app_id="999", secreto_app=SECRETO,
                           url_publica="https://x.com", contacto="x", tienda_esperada="5555",
                           http=httpx.Client(transport=httpx.MockTransport(tn)), tareas_de_fondo=False)
    assert TestClient(a).get("/tiendanube/instalada?code=codigo-bueno").status_code == 403


# ---------------------------------------------------------------- instalación y productos
def test_instalar_trae_productos_y_pide_avisos(c, tn):
    r = conectar(c)
    assert "4 productos" in r.text
    assert sorted(w["event"] for w in tn.webhooks) == ["app/uninstalled", "order/cancelled", "order/paid"]
    assert all(w["url"] == "https://tienda.ejemplo.com/tiendanube/aviso" for w in tn.webhooks)
    ps = {p["sku"]: p for p in c.get("/api/productos", headers=ADMIN).json()}
    assert ps["VENT-01"]["stock"] == 8 and ps["VENT-01"]["precio"] == 45000
    assert ps["FUN-ROJ"]["nombre"] == "Funda celular (Roja)"
    assert ps["STK"]["controla_stock"] == 0
    # Volver a revisar no duplica avisos y traer de nuevo no duplica productos
    c.post("/api/revisar-conexion", headers=ADMIN)
    assert len(tn.webhooks) == 3
    assert c.post("/api/productos/importar", headers=ADMIN).json() == {"nuevos": 0, "actualizados": 4}
    assert tn.stock_enviado == []  # traer productos no le manda stock a Tiendanube


def test_importar_une_por_sku_un_producto_cargado_a_mano(c, tn):
    base = c.app_.state.base
    base.ejec("INSERT INTO productos (nombre, sku, stock, modificado) VALUES ('Ventilador viejo', 'VENT-01', 3, 0)")
    conectar(c)
    ps = [p for p in c.get("/api/productos", headers=ADMIN).json() if p["sku"] == "VENT-01"]
    assert len(ps) == 1 and ps[0]["tn_variante_id"] == 101 and ps[0]["stock"] == 3  # el stock de acá manda


# ---------------------------------------------------------------- ventas web
def test_pedido_pagado_registra_venta_y_descuenta_stock(c, tn):
    conectar(c)
    c.patch(f"/api/productos/{prod(c, 'VENT-01')['id']}", json={"costo": 30000}, headers=ADMIN)
    tn.pedidos[5001] = pedido(5001, [{"product_id": 10, "variant_id": 101, "sku": "VENT-01", "name": "Ventilador de pie", "quantity": 2, "price": "45000.00"},
                                     {"product_id": 20, "variant_id": 202, "sku": "FUN-ROJ", "name": "Funda (Roja)", "quantity": 1, "price": "6000.00"}])
    assert avisar(c, "order/paid", 5001).status_code == 200
    assert prod(c, "VENT-01")["stock"] == 6
    assert prod(c, "FUN-ROJ")["stock"] == 11
    (v,) = c.get("/api/ventas", headers=ADMIN).json()
    assert v["origen"] == "web" and v["pedido_numero"] == "4001" and v["total"] == 99000 and v["envio"] == 3000
    assert v["costo"] == 60000 and v["medio_pago"] == "Mercado Pago" and v["cliente"]["nombre"] == "Ana" and not v["revisar"]
    # Tiendanube ya descontó su stock: no se le manda nada
    c.app_.state.empujar_stock()
    assert tn.stock_enviado == []
    e = c.get("/api/estado", headers=ADMIN).json()
    assert e["mes"]["n"] == 1 and e["mes"]["ganancia"] == 99000 - 3000 - 60000


def test_mismo_pedido_dos_veces_cuenta_una(c, tn):
    conectar(c)
    tn.pedidos[5002] = pedido(5002, [{"variant_id": 101, "sku": "VENT-01", "name": "V", "quantity": 1, "price": "45000"}])
    avisar(c, "order/paid", 5002)
    avisar(c, "order/paid", 5002)
    assert len(c.get("/api/ventas", headers=ADMIN).json()) == 1
    assert prod(c, "VENT-01")["stock"] == 7


def test_pedido_no_pagado_se_ignora(c, tn):
    conectar(c)
    tn.pedidos[5003] = pedido(5003, [{"variant_id": 101, "sku": "VENT-01", "name": "V", "quantity": 1, "price": "1"}], pago="pending")
    avisar(c, "order/paid", 5003)
    assert c.get("/api/ventas", headers=ADMIN).json() == []
    assert c.get("/api/avisos", headers=ADMIN).json()[0]["estado"] == "ignorado"


def test_producto_desconocido_queda_para_revisar(c, tn):
    conectar(c)
    tn.pedidos[5004] = pedido(5004, [{"variant_id": 999, "sku": "NO-EXISTE", "name": "Algo nuevo", "quantity": 1, "price": "100"},
                                     {"variant_id": None, "sku": "FUN-NEG", "name": "Funda", "quantity": 3, "price": "6000"}])
    avisar(c, "order/paid", 5004)
    (v,) = c.get("/api/ventas", headers=ADMIN).json()
    assert v["revisar"] == 1 and v["items"][0]["sin_producto"] and not v["items"][1]["sin_producto"]
    assert prod(c, "FUN-NEG")["stock"] == 27  # encontrado por SKU
    assert c.get("/api/ventas?revisar=true", headers=ADMIN).json()[0]["id"] == v["id"]


def test_pedido_cancelado_devuelve_stock(c, tn):
    conectar(c)
    tn.pedidos[5005] = pedido(5005, [{"variant_id": 101, "sku": "VENT-01", "name": "V", "quantity": 3, "price": "45000"}])
    avisar(c, "order/paid", 5005)
    assert prod(c, "VENT-01")["stock"] == 5
    avisar(c, "order/cancelled", 5005)
    avisar(c, "order/cancelled", 5005)  # repetido: no devuelve dos veces
    assert prod(c, "VENT-01")["stock"] == 8
    assert c.get("/api/ventas", headers=ADMIN).json()[0]["anulada"] == 1
    assert c.get("/api/estado", headers=ADMIN).json()["mes"]["n"] == 0
    c.app_.state.empujar_stock()
    assert tn.stock_enviado == []  # Tiendanube devuelve su propio stock al cancelar


def test_si_tiendanube_falla_el_aviso_se_reintenta(c, tn):
    conectar(c)
    avisar(c, "order/paid", 5006)  # el pedido todavía no "existe": 404
    av = c.get("/api/avisos", headers=ADMIN).json()[0]
    assert av["estado"] == "pendiente" and av["intentos"] == 1 and "404" in av["ultimo_error"]
    tn.pedidos[5006] = pedido(5006, [{"variant_id": 101, "sku": "VENT-01", "name": "V", "quantity": 1, "price": "45000"}])
    c.app_.state.reintentar_pendientes()
    assert c.get("/api/avisos", headers=ADMIN).json()[0]["estado"] == "hecho"
    assert prod(c, "VENT-01")["stock"] == 7


def test_aviso_que_falla_siempre_queda_en_error(c, tn):
    conectar(c)
    avisar(c, "order/paid", 777)
    for _ in range(conector.MAX_INTENTOS):
        c.app_.state.reintentar_pendientes()
    av = c.get("/api/avisos", headers=ADMIN).json()[0]
    assert av["estado"] == "error"
    assert c.get("/api/estado", headers=ADMIN).json()["avisos_error"] == 1


def test_desinstalar_borra_la_llave(c, tn):
    conectar(c)
    avisar(c, "app/uninstalled", None)
    assert c.get("/api/estado", headers=ADMIN).json()["conectada"] is False


# ---------------------------------------------------------------- mostrador y stock hacia Tiendanube
def test_venta_mostrador_descuenta_y_avisa_a_tiendanube(c, tn):
    conectar(c)
    p = prod(c, "FUN-NEG")
    r = c.post("/api/ventas", json={"items": [{"producto_id": p["id"], "cantidad": 4}], "medio_pago": "Efectivo", "descuento": 1000}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 23000
    assert prod(c, "FUN-NEG")["stock"] == 26
    assert tn.stock_enviado == [(20, {"action": "replace", "value": 26, "id": 201})]
    assert prod(c, "FUN-NEG")["sincronizado"] == 1
    # Anular devuelve el stock y también lo avisa
    assert c.post(f"/api/ventas/{r.json()['id']}/anular", headers=ADMIN).status_code == 200
    assert prod(c, "FUN-NEG")["stock"] == 30
    assert tn.stock_enviado[-1] == (20, {"action": "replace", "value": 30, "id": 201})
    assert c.post(f"/api/ventas/{r.json()['id']}/anular", headers=ADMIN).status_code == 409


def test_venta_web_no_se_anula_desde_el_panel(c, tn):
    conectar(c)
    v = c.post("/api/simular-pedido", headers=ADMIN).json()
    assert c.post(f"/api/ventas/{v['venta_id']}/anular", headers=ADMIN).status_code == 422


def test_ajuste_de_stock_se_manda_y_si_falla_queda_pendiente(c, tn):
    conectar(c)
    p = prod(c, "VENT-01")
    tn.falla_stock = True
    r = c.post(f"/api/productos/{p['id']}/ajuste", json={"stock_real": 5, "motivo": "Conteo"}, headers=ADMIN)
    assert r.status_code == 200 and prod(c, "VENT-01")["stock"] == 5
    assert prod(c, "VENT-01")["sincronizado"] == 0
    assert c.get("/api/estado", headers=ADMIN).json()["sin_sincronizar"] == 1
    tn.falla_stock = False
    c.app_.state.reintentar_pendientes()
    assert tn.stock_enviado == [(10, {"action": "replace", "value": 5, "id": 101})]
    movs = c.get(f"/api/productos/{p['id']}/movimientos", headers=ADMIN).json()
    assert movs[0]["tipo"] == "Ajuste" and movs[0]["cantidad"] == -3


def test_comparar_y_sincronizar_stock(c, tn):
    conectar(c)
    c.app_.state.base.ejec("UPDATE productos SET stock = 2 WHERE sku = 'FUN-ROJ'")
    d = c.get("/api/diferencias", headers=ADMIN).json()
    assert [(x["sku"], x["aca"], x["tiendanube"]) for x in d] == [("FUN-ROJ", 2, 12)]
    r = c.post("/api/sincronizar", headers=ADMIN).json()
    assert r == {"enviados": 3, "errores": 0}  # los 3 que controlan stock


# ---------------------------------------------------------------- privacidad
def test_borrado_de_datos_de_cliente(c, tn):
    conectar(c)
    tn.pedidos[5007] = pedido(5007, [{"variant_id": 101, "sku": "VENT-01", "name": "V", "quantity": 1, "price": "45000"}])
    avisar(c, "order/paid", 5007)
    cuerpo = json.dumps({"store_id": int(TIENDA), "customer": {"id": 7, "email": "ana@ejemplo.com"}, "orders_to_redact": [5007]}).encode()
    r = c.post("/tiendanube/privacidad/customers-redact", content=cuerpo, headers={"x-linkedstore-hmac-sha256": firmar(cuerpo)})
    assert r.status_code == 200
    v = c.get("/api/ventas", headers=ADMIN).json()[0]
    assert v["cliente"] == {"nombre": "(borrado a pedido)"} and v["total"] > 0
    assert c.post("/tiendanube/privacidad/customers-redact", content=cuerpo, headers={"x-linkedstore-hmac-sha256": "mala"}).status_code == 401


def test_simular_sin_tienda(c):
    c.app_.state.base.ejec("INSERT INTO productos (nombre, sku, precio, stock, modificado) VALUES ('Mate', 'MATE', 9000, 5, 0)")
    r = c.post("/api/simular-pedido", headers=ADMIN)
    assert r.status_code == 200
    assert c.get("/api/ventas", headers=ADMIN).json()[0]["medio_pago"] == "Simulado"
    assert prod(c, "MATE")["stock"] == 4


def test_producto_solo_aca_no_queda_pendiente_de_tiendanube(c):
    c.app_.state.base.ejec("INSERT INTO productos (nombre, sku, precio, stock, modificado) VALUES ('Mate', 'MATE', 9000, 5, 0)")
    p = prod(c, "MATE")
    assert c.post("/api/ventas", json={"items": [{"producto_id": p["id"], "cantidad": 1}]}, headers=ADMIN).status_code == 200
    assert prod(c, "MATE")["stock"] == 4 and prod(c, "MATE")["sincronizado"] == 1
