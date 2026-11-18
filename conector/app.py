"""Conector entre la tienda Tiendanube y el sistema de stock y ventas de la clienta.

Qué hace:
- Se instala en la tienda como una app de Tiendanube y trae los productos (cada variante con su SKU).
- Cuando entra un pedido PAGADO, Tiendanube avisa; el conector busca el pedido, registra la venta
  y descuenta el stock. Si el pedido se cancela, la anula y devuelve el stock.
- Ventas de mostrador y ajustes de stock hechos acá se mandan a Tiendanube, para no vender lo que no hay.
- Panel web en /panel para ver ventas, ganancia y stock.

Cómo se corre (desarrollo):
    pip install -r requirements.txt
    ADMIN_TOKEN=un-token-largo-de-prueba uvicorn app:app --reload
    (abrir http://localhost:8000/panel)

En producción va detrás de HTTPS (ver deploy/ y docs/PASOS.md).
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

try:  # funciona como paquete (tests) y como script (uvicorn app:app)
    from . import tiendanube as tn
    from .base import Base, ahora_ms, fila
except ImportError:  # pragma: no cover
    import tiendanube as tn
    from base import Base, ahora_ms, fila

log = logging.getLogger("conector")
AQUI = pathlib.Path(__file__).resolve().parent
MAX_INTENTOS = 12           # un aviso que falla 12 veces queda en "error" para mirarlo a mano
SEGUNDOS_REINTENTO = 60     # cada cuánto se reintentan avisos y stock pendientes


def iso_a_ms(texto: str | None) -> int | None:
    if not texto:
        return None
    try:
        return int(datetime.fromisoformat(texto.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def num(x, defecto: float = 0.0) -> float:
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return defecto


def texto_idioma(x) -> str:
    """Tiendanube manda los nombres por idioma: {"es": "Ventilador"}."""
    if isinstance(x, dict):
        return str(x.get("es") or next(iter(x.values()), "") or "")
    return str(x or "")


def _texto(x) -> str:
    if isinstance(x, dict):
        return str(x.get("name") or x.get("es") or x.get("code") or "")
    return "" if x is None else str(x)


def detalle_pedido(p: dict) -> dict:
    """Todo lo útil de un pedido de Tiendanube, ordenado para mostrarlo en el panel."""
    dir_ = p.get("shipping_address") or {}
    direccion = " ".join(x for x in (_texto(dir_.get("address")), _texto(dir_.get("number"))) if x)
    if dir_.get("floor"):
        direccion += f", piso/depto {dir_['floor']}"
    pago = p.get("payment_details") or {}
    cupones = [_texto(c.get("code") if isinstance(c, dict) else c) for c in (p.get("coupon") or [])]
    return {
        "creado_ts": iso_a_ms(p.get("created_at")),
        "pagado_ts": iso_a_ms(p.get("paid_at")),
        "enviado_ts": iso_a_ms(p.get("shipped_at")),
        "cancelado_ts": iso_a_ms(p.get("cancelled_at")),
        "estado": _texto(p.get("status")),
        "estado_pago": _texto(p.get("payment_status")),
        "estado_envio": _texto(p.get("shipping_status")),
        "origen": _texto(p.get("storefront")),
        "moneda": _texto(p.get("currency")),
        "envio": {
            "metodo": _texto(p.get("shipping_option")) or _texto(p.get("shipping")),
            "transportista": _texto(p.get("shipping_carrier_name")),
            "retiro": _texto(p.get("shipping_pickup_type")) == "pickup",
            "cobrado": num(p.get("shipping_cost_customer")),
            "costo_tienda": num(p.get("shipping_cost_owner")) if p.get("shipping_cost_owner") not in (None, "") else None,
            "seguimiento": _texto(p.get("shipping_tracking_number")),
            "seguimiento_url": _texto(p.get("shipping_tracking_url")),
            "destinatario": _texto(dir_.get("name")),
            "telefono": _texto(dir_.get("phone")),
            "direccion": direccion,
            "localidad": _texto(dir_.get("locality")),
            "ciudad": _texto(dir_.get("city")),
            "provincia": _texto(dir_.get("province")),
            "cp": _texto(dir_.get("zipcode")),
        },
        "pago": {
            "plataforma": _texto(p.get("gateway_name")) or _texto(p.get("gateway")),
            "metodo": _texto(pago.get("method")),
            "tarjeta": _texto(pago.get("credit_card_company")),
            "cuotas": pago.get("installments"),
            "operacion": _texto(p.get("gateway_id")),
        },
        "descuentos": {
            "total": num(p.get("discount")),
            "cupon": num(p.get("discount_coupon")),
            "medio_pago": num(p.get("discount_gateway")),
            "cupones": [c for c in cupones if c],
        },
        "facturacion": {
            "nombre": _texto(p.get("billing_name")),
            "documento": _texto(p.get("contact_identification")),
            "direccion": " ".join(x for x in (_texto(p.get("billing_address")), _texto(p.get("billing_number"))) if x),
            "ciudad": _texto(p.get("billing_city")),
            "provincia": _texto(p.get("billing_province")),
            "cp": _texto(p.get("billing_zipcode")),
        },
        "nota_cliente": _texto(p.get("note")),
        "nota_tienda": _texto(p.get("owner_note")),
    }


def inicio_dia(ms: int, tz_min: int) -> int:
    """Medianoche (en la hora del navegador) del día de ms. tz_min = minutos a sumar a UTC (Argentina: -180)."""
    dia = 86_400_000
    return (ms + tz_min * 60_000) // dia * dia - tz_min * 60_000


# ---------------------------------------------------------------- datos que llegan del panel
class Ajuste(BaseModel):
    stock_real: float = Field(ge=0)
    motivo: str = Field(min_length=1, max_length=120)


class CambiosProducto(BaseModel):
    costo: Optional[float] = Field(default=None, ge=0)
    minimo: Optional[float] = Field(default=None, ge=0)


class ItemMostrador(BaseModel):
    producto_id: int
    cantidad: float = Field(gt=0, le=100000)
    precio: Optional[float] = Field(default=None, ge=0)  # si falta, el precio cargado


class VentaMostrador(BaseModel):
    items: list[ItemMostrador] = Field(min_length=1, max_length=200)
    descuento: float = Field(default=0, ge=0)
    medio_pago: str = Field(default="Efectivo", min_length=1, max_length=60)
    cliente: str = Field(default="", max_length=120)
    telefono: str = Field(default="", max_length=40)
    email: str = Field(default="", max_length=160)
    nota: str = Field(default="", max_length=300)


class ItemCompra(BaseModel):
    producto_id: int
    cantidad: float = Field(gt=0, le=1_000_000)
    costo_unitario: float = Field(ge=0)


class Compra(BaseModel):
    """Mercadería que entra: suma stock, actualiza el costo y queda como inversión."""
    items: list[ItemCompra] = Field(min_length=1, max_length=200)
    proveedor: str = Field(default="", max_length=120)
    extra: float = Field(default=0, ge=0)  # flete, aduana, etc. Se reparte en el costo
    fecha_ms: Optional[int] = None


class MovPlata(BaseModel):
    tipo: str = Field(pattern="^(gasto|inversion)$")
    categoria: str = Field(default="", max_length=60)
    concepto: str = Field(min_length=1, max_length=160)
    monto: float = Field(gt=0)
    fecha_ms: Optional[int] = None


# ---------------------------------------------------------------- app
def crear_app(ruta_base: str | None = None, *, admin_token: str | None = None, app_id: str | None = None,
              secreto_app: str | None = None, url_publica: str | None = None, contacto: str | None = None,
              tienda_esperada: str | None = None, http: httpx.Client | None = None,
              tareas_de_fondo: bool = True) -> FastAPI:
    env = os.environ.get
    base = Base(ruta_base or env("CONECTOR_DB", "conector.db"))
    token = admin_token if admin_token is not None else env("ADMIN_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("Definí ADMIN_TOKEN con al menos 16 caracteres")
    app_id = app_id if app_id is not None else env("TN_APP_ID", "")
    secreto = secreto_app if secreto_app is not None else env("TN_APP_SECRETO", "")
    url_pub = (url_publica if url_publica is not None else env("URL_PUBLICA", "")).rstrip("/")
    contacto = contacto if contacto is not None else env("TN_CONTACTO", "")
    tienda_esperada = tienda_esperada if tienda_esperada is not None else env("TN_TIENDA_ID", "")
    http = http or httpx.Client(timeout=20)

    app = FastAPI(title="Conector Tiendanube", docs_url=None, redoc_url=None)
    app.state.base = base

    # ---------- conexión con la tienda
    def api() -> tn.API | None:
        tid, tok = base.get("tienda_id"), base.get("token")
        return tn.API(tid, tok, contacto or url_pub or "sin-contacto", http=http) if tid and tok else None

    def api_o_409() -> tn.API:
        a = api()
        if not a:
            raise HTTPException(409, "La tienda todavía no está conectada. Instalá la app desde el panel.")
        return a

    def admin(authorization: str = Header(default="")):
        if not secrets.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Falta la clave de administrador o es incorrecta")

    # ---------- productos
    def importar_productos() -> dict:
        """Trae productos y variantes de Tiendanube. Une por variante y, si no, por SKU.

        El stock de Tiendanube se toma solo para productos nuevos: después manda el de acá.
        """
        a = api_o_409()
        nuevos = actualizados = 0
        for p in a.productos():
            nombre_base = texto_idioma(p.get("name")) or f"Producto {p.get('id')}"
            for v in p.get("variants") or []:
                valores = " / ".join(texto_idioma(x) for x in (v.get("values") or []) if texto_idioma(x))
                nombre = f"{nombre_base} ({valores})" if valores else nombre_base
                sku = str(v.get("sku") or "").strip()
                controla = 0 if v.get("stock") in (None, "") or v.get("stock_management") is False else 1
                datos = (nombre, sku, str(v.get("barcode") or "").strip(), num(v.get("promotional_price") or v.get("price")),
                         int(p["id"]), int(v["id"]))
                with base.transaccion():
                    actual = base.uno("SELECT * FROM productos WHERE tn_variante_id = ?", (v["id"],))
                    if not actual and sku:
                        actual = base.uno("SELECT * FROM productos WHERE tn_variante_id IS NULL AND sku = ?", (sku,))
                    if actual:
                        base.ejec("UPDATE productos SET nombre=?, sku=?, codigo_barras=?, precio=?, tn_producto_id=?, tn_variante_id=?, "
                                  "activo=1, modificado=? WHERE id=?", datos + (ahora_ms(), actual["id"]))
                        actualizados += 1
                    else:
                        cur = base.ejec("INSERT INTO productos (nombre, sku, codigo_barras, precio, tn_producto_id, tn_variante_id, "
                                        "controla_stock, costo, modificado) VALUES (?,?,?,?,?,?,?,?,?)",
                                        datos + (controla, num(v.get("cost")), ahora_ms()))
                        if controla and num(v.get("stock")):
                            base.mover_stock(cur.lastrowid, num(v.get("stock")), "Stock inicial", "Traído de Tiendanube",
                                             avisar_tienda=False)
                        nuevos += 1
        base.set("ultima_importacion", str(ahora_ms()))
        return {"nuevos": nuevos, "actualizados": actualizados}

    def empujar_stock() -> dict:
        """Manda a Tiendanube el stock de los productos que cambiaron acá."""
        a = api()
        if not a:
            return {"enviados": 0, "errores": 0}
        enviados = errores = 0
        for p in base.q("SELECT * FROM productos WHERE sincronizado = 0 AND controla_stock = 1 AND activo = 1 "
                        "AND tn_variante_id IS NOT NULL"):
            try:
                a.poner_stock(p["tn_producto_id"], p["tn_variante_id"], max(p["stock"], 0))
            except Exception as e:  # queda pendiente y se reintenta
                errores += 1
                log.warning("No se pudo mandar el stock de %s: %s", p["nombre"], e)
                continue
            # Solo se marca si nadie lo cambió mientras tanto.
            base.ejec("UPDATE productos SET sincronizado = 1 WHERE id = ? AND modificado = ?", (p["id"], p["modificado"]))
            enviados += 1
        return {"enviados": enviados, "errores": errores}

    def buscar_producto(item: dict):
        if item.get("variant_id"):
            p = base.uno("SELECT * FROM productos WHERE tn_variante_id = ?", (item["variant_id"],))
            if p:
                return p
        sku = str(item.get("sku") or "").strip()
        if sku:
            return base.uno("SELECT * FROM productos WHERE activo = 1 AND (sku = ? OR codigo_barras = ?) ORDER BY id LIMIT 1", (sku, sku))
        return None

    # ---------- ventas
    def registrar_venta_web(pedido: dict, medio: str | None = None) -> dict:
        """Registra un pedido pagado de Tiendanube. Si ya estaba, no hace nada (no cuenta dos veces)."""
        pid = int(pedido["id"])
        with base.transaccion():
            ya = base.uno("SELECT id FROM ventas WHERE pedido_tn_id = ?", (pid,))
            if ya:
                return {"venta_id": ya["id"], "repetido": True}
            items, costo, revisar = [], 0.0, False
            for it in pedido.get("products") or []:
                p = buscar_producto(it)
                cant = num(it.get("quantity"), 1)
                img = it.get("image") or {}
                items.append({"producto_id": p["id"] if p else None, "nombre": str(it.get("name") or (p and p["nombre"]) or "?"),
                              "sku": str(it.get("sku") or ""), "cantidad": cant, "precio": num(it.get("price")),
                              "costo": p["costo"] if p else 0, "sin_producto": p is None,
                              "variante": " / ".join(texto_idioma(x) for x in (it.get("variant_values") or []) if texto_idioma(x)),
                              "imagen": img.get("src", "") if isinstance(img, dict) else ""})
                costo += (p["costo"] if p else 0) * cant
                revisar = revisar or p is None
            c = pedido.get("customer") or {}
            cliente = {"nombre": str(c.get("name") or pedido.get("contact_name") or ""),
                       "email": str(c.get("email") or pedido.get("contact_email") or ""),
                       "telefono": str(c.get("phone") or pedido.get("contact_phone") or ""),
                       "documento": str(pedido.get("contact_identification") or c.get("identification") or ""),
                       "tn_cliente_id": c.get("id")}
            det = detalle_pedido(pedido)
            if det["envio"]["ciudad"]:
                cliente["ciudad"] = det["envio"]["ciudad"]
                cliente["provincia"] = det["envio"]["provincia"]
            # Lo que paga la tienda por el envío. Si Tiendanube no lo dice, se toma lo que pagó el cliente (pasa de largo).
            envio_costo = det["envio"]["costo_tienda"] if det["envio"]["costo_tienda"] is not None else det["envio"]["cobrado"]
            ts = iso_a_ms(pedido.get("paid_at")) or iso_a_ms(pedido.get("created_at")) or ahora_ms()
            cur = base.ejec(
                "INSERT INTO ventas (ts, origen, pedido_tn_id, pedido_numero, items, subtotal, envio, descuento, total, costo, "
                "medio_pago, cliente, revisar, detalle, pedido_tn, envio_costo) VALUES (?, 'web', ?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, pid, str(pedido.get("number") or pid), json.dumps(items), num(pedido.get("subtotal")),
                 num(pedido.get("shipping_cost_customer")), num(pedido.get("discount")), num(pedido.get("total")),
                 round(costo, 2), medio or str(pedido.get("gateway_name") or pedido.get("gateway") or "Tienda web"),
                 json.dumps(cliente), int(revisar), json.dumps(det), json.dumps(pedido)[:200_000], envio_costo))
            venta_id = cur.lastrowid
            for i in items:
                if i["producto_id"]:
                    # Tiendanube ya descontó su stock: acá solo se descuenta el nuestro.
                    base.mover_stock(i["producto_id"], -i["cantidad"], "Venta web", f"Pedido #{pedido.get('number') or pid}",
                                     venta_id=venta_id, avisar_tienda=False, ts=ts)
        return {"venta_id": venta_id, "repetido": False, "revisar": revisar}

    def anular_venta(venta_id: int, motivo: str, avisar_tienda: bool) -> dict | None:
        with base.transaccion():
            v = base.uno("SELECT * FROM ventas WHERE id = ?", (venta_id,))
            if not v or v["anulada"]:
                return None
            base.ejec("UPDATE ventas SET anulada = 1, anulada_ts = ? WHERE id = ?", (ahora_ms(), venta_id))
            for i in json.loads(v["items"]):
                if i.get("producto_id"):
                    base.mover_stock(i["producto_id"], i["cantidad"], "Anulación", motivo, venta_id=venta_id,
                                     avisar_tienda=avisar_tienda)
        return fila(base.uno("SELECT * FROM ventas WHERE id = ?", (venta_id,)))

    # ---------- avisos de Tiendanube
    def procesar_aviso(aviso_id: int) -> str:
        av = base.uno("SELECT * FROM avisos WHERE id = ?", (aviso_id,))
        if not av or av["estado"] in ("hecho", "ignorado"):
            return av["estado"] if av else "no existe"
        estado, error = "hecho", ""
        try:
            if av["evento"] == "order/paid":
                pedido = api_o_409().pedido(av["recurso_id"])
                if pedido.get("payment_status") != "paid":
                    estado = "ignorado"
                else:
                    registrar_venta_web(pedido)
            elif av["evento"] == "order/cancelled":
                v = base.uno("SELECT id FROM ventas WHERE pedido_tn_id = ?", (av["recurso_id"],))
                # Al cancelar, Tiendanube devuelve su stock: acá solo se devuelve el nuestro.
                if not v or not anular_venta(v["id"], f"Pedido cancelado en Tiendanube", avisar_tienda=False):
                    estado = "ignorado"
            elif av["evento"] in ("order/updated", "order/fulfilled"):
                # Solo actualiza los datos (estado del envío, seguimiento, notas). El stock no se toca.
                v = base.uno("SELECT id FROM ventas WHERE pedido_tn_id = ?", (av["recurso_id"],))
                if not v:
                    estado = "ignorado"
                else:
                    pedido = api_o_409().pedido(av["recurso_id"])
                    base.ejec("UPDATE ventas SET detalle = ?, pedido_tn = ? WHERE id = ?",
                              (json.dumps(detalle_pedido(pedido)), json.dumps(pedido)[:200_000], v["id"]))
            elif av["evento"] == "app/uninstalled":
                base.borrar("token")
            else:
                estado = "ignorado"
        except Exception as e:
            estado, error = "pendiente", str(e)[:500]
            log.warning("Aviso %s (%s) falló: %s", aviso_id, av["evento"], e)
        intentos = av["intentos"] + 1
        if estado == "pendiente" and intentos >= MAX_INTENTOS:
            estado = "error"
        base.ejec("UPDATE avisos SET estado = ?, intentos = ?, ultimo_error = ?, procesado_ts = ? WHERE id = ?",
                  (estado, intentos, error, ahora_ms(), aviso_id))
        return estado

    def reintentar_pendientes() -> None:
        for av in base.q("SELECT id FROM avisos WHERE estado = 'pendiente' ORDER BY id LIMIT 50"):
            procesar_aviso(av["id"])
        empujar_stock()

    async def cuerpo_firmado(request: Request) -> dict:
        cuerpo = await request.body()
        if not tn.firma_valida(secreto, cuerpo, request.headers.get("x-linkedstore-hmac-sha256", "")):
            raise HTTPException(401, "Firma inválida")
        try:
            datos = json.loads(cuerpo)
        except ValueError:
            raise HTTPException(422, "El aviso no es JSON")
        tienda = str(datos.get("store_id") or "")
        if base.get("tienda_id") and tienda and tienda != base.get("tienda_id"):
            raise HTTPException(403, "Ese aviso es de otra tienda")
        return datos

    @app.post("/tiendanube/aviso")
    async def aviso(request: Request, fondo: BackgroundTasks):
        """Tiendanube llama acá con {store_id, event, id}. Hay que contestar en menos de 3 segundos,
        así que se guarda el aviso y se procesa después."""
        datos = await cuerpo_firmado(request)
        cur = base.ejec("INSERT INTO avisos (recibido_ts, evento, recurso_id, cuerpo) VALUES (?,?,?,?)",
                        (ahora_ms(), str(datos.get("event") or ""), datos.get("id"), json.dumps(datos)[:4000]))
        fondo.add_task(procesar_aviso, cur.lastrowid)
        return {"ok": True}

    # Avisos de privacidad que Tiendanube exige a toda app (se cargan en el portal de Partners).
    @app.post("/tiendanube/privacidad/{tipo}")
    async def privacidad(tipo: str, request: Request):
        if tipo not in ("store-redact", "customers-redact", "customers-data-request"):
            raise HTTPException(404)
        datos = await cuerpo_firmado(request)
        base.ejec("INSERT INTO avisos (recibido_ts, evento, recurso_id, cuerpo, estado) VALUES (?,?,?,?,?)",
                  (ahora_ms(), "privacidad/" + tipo, None, json.dumps(datos)[:4000], "hecho"))
        if tipo == "customers-redact":
            # Borra los datos personales del cliente en las ventas (los números quedan para la contabilidad).
            c = datos.get("customer") or {}
            for v in base.q("SELECT id, cliente FROM ventas WHERE origen = 'web'"):
                cl = json.loads(v["cliente"])
                if (c.get("id") and cl.get("tn_cliente_id") == c.get("id")) or (c.get("email") and cl.get("email") == c.get("email")):
                    base.ejec("UPDATE ventas SET cliente = ? WHERE id = ?", (json.dumps({"nombre": "(borrado a pedido)"}), v["id"]))
        elif tipo == "store-redact":
            base.borrar("token")
        # customers-data-request: queda anotado en avisos; los datos se mandan a mano a la clienta.
        return {"ok": True}

    # ---------- instalación
    @app.post("/api/instalar", dependencies=[Depends(admin)])
    def instalar():
        if not app_id or not secreto:
            raise HTTPException(503, "Faltan TN_APP_ID y TN_APP_SECRETO en la configuración del servidor")
        estado = secrets.token_urlsafe(24)
        base.set("estado_instalacion", estado)
        return {"url": tn.url_instalar(app_id, estado)}

    @app.get("/tiendanube/instalada", response_class=HTMLResponse)
    def instalada(code: str = "", state: str = ""):
        """Tiendanube vuelve acá después de que la clienta acepta la app (URL de redirección de la app)."""
        esperado = base.get("estado_instalacion")
        if state:
            if not esperado or not secrets.compare_digest(state, esperado):
                raise HTTPException(400, "El enlace de instalación venció. Volvé a empezar desde el panel.")
        elif base.get("token") and not tienda_esperada:
            raise HTTPException(400, "Ya hay una tienda conectada. Para cambiarla, empezá desde el panel.")
        if not code:
            raise HTTPException(400, "Falta el código de Tiendanube")
        datos = tn.canjear_codigo(app_id, secreto, code, http=http)
        if tienda_esperada and datos["tienda_id"] != tienda_esperada:
            raise HTTPException(403, "Esta app solo se puede instalar en la tienda configurada")
        base.set("tienda_id", datos["tienda_id"])
        base.set("token", datos["token"])
        base.borrar("estado_instalacion")
        mensaje = "Tienda conectada."
        try:
            if url_pub.startswith("https://"):
                api().registrar_avisos(url_pub + "/tiendanube/aviso")
            else:
                mensaje += " Ojo: falta URL_PUBLICA con https, así que Tiendanube todavía no puede avisar las ventas."
            r = importar_productos()
            mensaje += f" Se trajeron {r['nuevos']} productos."
        except Exception as e:
            mensaje += f" Hubo un problema al terminar de configurar: {e}. Probá 'Revisar conexión' en el panel."
        return f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>Conector</title>" \
               f"<body style='font-family:system-ui;max-width:520px;margin:40px auto;padding:0 16px'><h2>Listo</h2>" \
               f"<p>{mensaje}</p><p><a href='/panel'>Ir al panel</a></p>"

    @app.post("/api/revisar-conexion", dependencies=[Depends(admin)])
    def revisar_conexion():
        a = api_o_409()
        avisos = a.registrar_avisos(url_pub + "/tiendanube/aviso") if url_pub.startswith("https://") else []
        return {"ok": True, "avisos": avisos, **empujar_stock()}

    # ---------- panel
    @app.get("/salud", include_in_schema=False)
    def salud():
        base.uno("SELECT 1")
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def raiz():
        return RedirectResponse("/panel")

    @app.get("/panel", include_in_schema=False)
    def panel():
        return FileResponse(AQUI / "panel.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    def inicios(tz: int) -> tuple[int, int]:
        """(medianoche de hoy, día 1 del mes) en la hora del navegador, en ms."""
        local = datetime.fromtimestamp((ahora_ms() + tz * 60_000) / 1000, timezone.utc)
        hoy = local.replace(hour=0, minute=0, second=0, microsecond=0)
        a_ms = lambda d: int(d.timestamp() * 1000) - tz * 60_000
        return a_ms(hoy), a_ms(hoy.replace(day=1))

    @app.get("/api/estado", dependencies=[Depends(admin)])
    def estado(tz: int = -180):
        desde_hoy, desde_mes = inicios(tz)

        def totales(desde):
            f = base.uno("SELECT COUNT(*) n, COALESCE(SUM(total),0) total, COALESCE(SUM(total - envio_costo - costo),0) ganancia "
                         "FROM ventas WHERE anulada = 0 AND ts >= ?", (desde,))
            return dict(f)
        return {
            "conectada": bool(base.get("token")),
            "tienda_id": base.get("tienda_id"),
            "configurada": bool(app_id and secreto),
            "url_avisos": (url_pub + "/tiendanube/aviso") if url_pub else "",
            "ultima_importacion": int(base.get("ultima_importacion") or 0) or None,
            "hoy": totales(desde_hoy),
            "mes": totales(desde_mes),
            "stock_bajo": base.uno("SELECT COUNT(*) n FROM productos WHERE activo=1 AND controla_stock=1 AND stock <= minimo")["n"],
            "sin_sincronizar": base.uno("SELECT COUNT(*) n FROM productos WHERE sincronizado=0 AND tn_variante_id IS NOT NULL")["n"],
            "ventas_revisar": base.uno("SELECT COUNT(*) n FROM ventas WHERE revisar=1 AND anulada=0")["n"],
            "avisos_error": base.uno("SELECT COUNT(*) n FROM avisos WHERE estado IN ('error','pendiente') AND intentos > 0")["n"],
        }

    @app.get("/api/productos", dependencies=[Depends(admin)])
    def productos():
        return [dict(p) for p in base.q("SELECT * FROM productos WHERE activo = 1 ORDER BY nombre")]

    @app.post("/api/productos/importar", dependencies=[Depends(admin)])
    def importar():
        return importar_productos()

    @app.patch("/api/productos/{producto_id}", dependencies=[Depends(admin)])
    def cambiar_producto(producto_id: int, d: CambiosProducto):
        if not base.uno("SELECT 1 FROM productos WHERE id = ?", (producto_id,)):
            raise HTTPException(404, "No existe ese producto")
        for campo in ("costo", "minimo"):
            if getattr(d, campo) is not None:
                base.ejec(f"UPDATE productos SET {campo} = ? WHERE id = ?", (getattr(d, campo), producto_id))
        return dict(base.uno("SELECT * FROM productos WHERE id = ?", (producto_id,)))

    @app.post("/api/productos/{producto_id}/ajuste", dependencies=[Depends(admin)])
    def ajustar(producto_id: int, d: Ajuste, fondo: BackgroundTasks):
        p = base.uno("SELECT * FROM productos WHERE id = ?", (producto_id,))
        if not p:
            raise HTTPException(404, "No existe ese producto")
        if not p["controla_stock"]:
            raise HTTPException(422, "Ese producto no controla stock")
        dif = round(d.stock_real - p["stock"], 3)
        if dif:
            base.mover_stock(producto_id, dif, "Ajuste", f"{d.motivo} (había {p['stock']:g}, se contaron {d.stock_real:g})")
            fondo.add_task(empujar_stock)
        return dict(base.uno("SELECT * FROM productos WHERE id = ?", (producto_id,)))

    @app.get("/api/productos/{producto_id}/movimientos", dependencies=[Depends(admin)])
    def movimientos(producto_id: int, limite: int = 100):
        return [dict(m) for m in base.q("SELECT * FROM movimientos WHERE producto_id = ? ORDER BY id DESC LIMIT ?",
                                        (producto_id, min(max(limite, 1), 1000)))]

    @app.get("/api/diferencias", dependencies=[Depends(admin)])
    def diferencias():
        """Compara el stock de acá con el de Tiendanube."""
        tn_stock = {}
        for p in api_o_409().productos():
            for v in p.get("variants") or []:
                tn_stock[int(v["id"])] = v.get("stock")
        out = []
        for p in base.q("SELECT * FROM productos WHERE activo=1 AND controla_stock=1 AND tn_variante_id IS NOT NULL"):
            allá = tn_stock.get(p["tn_variante_id"], "no está")
            if allá == "no está" or allá is None or num(allá) != num(p["stock"]):
                out.append({"producto_id": p["id"], "nombre": p["nombre"], "sku": p["sku"], "aca": p["stock"], "tiendanube": allá})
        return out

    @app.post("/api/sincronizar", dependencies=[Depends(admin)])
    def sincronizar():
        """Manda TODO el stock de acá a Tiendanube (para corregir diferencias)."""
        base.ejec("UPDATE productos SET sincronizado = 0 WHERE controla_stock = 1 AND tn_variante_id IS NOT NULL")
        return empujar_stock()

    @app.get("/api/ventas", dependencies=[Depends(admin)])
    def ventas(origen: Optional[str] = None, revisar: bool = False, limite: int = 200):
        sql, args = "SELECT * FROM ventas WHERE 1=1", []
        if origen in ("web", "mostrador"):
            sql += " AND origen = ?"
            args.append(origen)
        if revisar:
            sql += " AND revisar = 1 AND anulada = 0"
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(min(max(limite, 1), 1000))
        return [fila(v) for v in base.q(sql, args)]

    @app.post("/api/ventas", dependencies=[Depends(admin)])
    def vender(d: VentaMostrador, fondo: BackgroundTasks):
        """Venta de mostrador: descuenta stock acá y lo manda a Tiendanube."""
        with base.transaccion():
            items, costo = [], 0.0
            for i in d.items:
                p = base.uno("SELECT * FROM productos WHERE id = ? AND activo = 1", (i.producto_id,))
                if not p:
                    raise HTTPException(404, f"No existe el producto {i.producto_id}")
                precio = p["precio"] if i.precio is None else i.precio
                items.append({"producto_id": p["id"], "nombre": p["nombre"], "sku": p["sku"], "cantidad": i.cantidad,
                              "precio": round(precio, 2), "costo": p["costo"], "sin_producto": False})
                costo += p["costo"] * i.cantidad
            subtotal = round(sum(x["precio"] * x["cantidad"] for x in items), 2)
            total = round(max(subtotal - d.descuento, 0), 2)
            cur = base.ejec("INSERT INTO ventas (ts, origen, items, subtotal, descuento, total, costo, medio_pago, cliente, detalle) "
                            "VALUES (?, 'mostrador', ?,?,?,?,?,?,?,?)",
                            (ahora_ms(), json.dumps(items), subtotal, d.descuento, total, round(costo, 2), d.medio_pago,
                             json.dumps({"nombre": d.cliente.strip(), "telefono": d.telefono.strip(), "email": d.email.strip().lower()}),
                             json.dumps({"nota_tienda": d.nota.strip()})))
            for x in items:
                base.mover_stock(x["producto_id"], -x["cantidad"], "Venta mostrador", f"Venta {cur.lastrowid}", venta_id=cur.lastrowid)
        fondo.add_task(empujar_stock)
        return fila(base.uno("SELECT * FROM ventas WHERE id = ?", (cur.lastrowid,)))

    @app.post("/api/ventas/{venta_id}/anular", dependencies=[Depends(admin)])
    def anular(venta_id: int, fondo: BackgroundTasks):
        v = base.uno("SELECT * FROM ventas WHERE id = ?", (venta_id,))
        if not v:
            raise HTTPException(404, "No existe esa venta")
        if v["origen"] == "web":
            raise HTTPException(422, "Las ventas web se cancelan desde Tiendanube; el conector se entera solo")
        r = anular_venta(venta_id, f"Anula venta {venta_id}", avisar_tienda=True)
        if not r:
            raise HTTPException(409, "Esa venta ya estaba anulada")
        fondo.add_task(empujar_stock)
        return r

    @app.get("/api/avisos", dependencies=[Depends(admin)])
    def avisos(limite: int = 100):
        return [dict(a) for a in base.q("SELECT * FROM avisos ORDER BY id DESC LIMIT ?", (min(max(limite, 1), 1000),))]

    @app.post("/api/avisos/{aviso_id}/reintentar", dependencies=[Depends(admin)])
    def reintentar(aviso_id: int):
        base.ejec("UPDATE avisos SET estado = 'pendiente', intentos = 0 WHERE id = ? AND estado = 'error'", (aviso_id,))
        return {"estado": procesar_aviso(aviso_id)}

    @app.post("/api/simular-pedido", dependencies=[Depends(admin)])
    def simular_pedido():
        """Para probar sin tienda: arma un pedido pagado con 1 o 2 productos cargados y lo registra como venta web."""
        ps = base.q("SELECT * FROM productos WHERE activo = 1 ORDER BY RANDOM() LIMIT 2")
        if not ps:
            raise HTTPException(409, "Primero tiene que haber productos")
        n = base.uno("SELECT COUNT(*) n FROM ventas WHERE pedido_tn_id < 0")["n"] + 1
        prods = [{"variant_id": p["tn_variante_id"], "sku": p["sku"], "name": p["nombre"], "quantity": 1, "price": p["precio"]} for p in ps]
        subtotal = sum(x["price"] for x in prods)
        import random
        nombre, tel, ciudad = random.choice([("Ana Gómez", "2615551234", "Mendoza"), ("Juan Pérez", "2614449876", "Godoy Cruz"),
                                             ("Lucía Funes", "2616667788", "Luján de Cuyo")])
        envio = random.choice([0, 3500])
        ahora = datetime.now(timezone.utc)
        pedido = {"id": -n, "number": f"PRUEBA-{n}", "payment_status": "paid", "status": "open", "shipping_status": "unpacked",
                  "products": prods, "subtotal": subtotal, "shipping_cost_customer": envio, "discount": 0, "total": subtotal + envio,
                  "created_at": (ahora - timedelta(minutes=3)).isoformat(), "paid_at": ahora.isoformat(), "storefront": "store",
                  "customer": {"name": nombre, "email": nombre.split()[0].lower() + "@ejemplo.com", "phone": tel},
                  "shipping_option": "Retiro en el local" if not envio else "Envío a domicilio (48 hs)",
                  "shipping_address": {"name": nombre, "address": "San Martín", "number": "1234", "city": ciudad, "province": "Mendoza",
                                       "zipcode": "5500", "phone": tel},
                  "payment_details": {"method": "credit_card", "credit_card_company": "visa", "installments": 3},
                  "gateway_name": "Mercado Pago (simulado)", "gateway_id": f"SIM-{n}", "note": "Pedido de prueba"}
        return registrar_venta_web(pedido)

    @app.get("/api/ventas/{venta_id}/pedido", dependencies=[Depends(admin)])
    def pedido_original(venta_id: int):
        """El pedido completo tal como lo mandó Tiendanube (todos los campos)."""
        v = base.uno("SELECT pedido_tn FROM ventas WHERE id = ?", (venta_id,))
        if not v:
            raise HTTPException(404, "No existe esa venta")
        return json.loads(v["pedido_tn"]) if v["pedido_tn"] else {}

    # ---------- compras de mercadería, gastos e inversiones
    @app.post("/api/compras", dependencies=[Depends(admin)])
    def comprar(d: Compra, fondo: BackgroundTasks):
        """Entra mercadería: suma stock (y lo manda a Tiendanube), recalcula el costo promedio y queda como compra."""
        ts = d.fecha_ms or ahora_ms()
        bruto = sum(i.cantidad * i.costo_unitario for i in d.items)
        with base.transaccion():
            items = []
            for i in d.items:
                p = base.uno("SELECT * FROM productos WHERE id = ? AND activo = 1", (i.producto_id,))
                if not p:
                    raise HTTPException(404, f"No existe el producto {i.producto_id}")
                # El flete/aduana se reparte según lo que pesa cada producto en la compra.
                extra_u = (d.extra * (i.cantidad * i.costo_unitario) / bruto / i.cantidad) if bruto else d.extra / sum(x.cantidad for x in d.items)
                costo_u = round(i.costo_unitario + extra_u, 2)
                previo = max(p["stock"], 0) if p["controla_stock"] else 0
                nuevo_costo = round((previo * p["costo"] + i.cantidad * costo_u) / (previo + i.cantidad), 2) if previo else costo_u
                base.ejec("UPDATE productos SET costo = ? WHERE id = ?", (nuevo_costo, p["id"]))
                items.append({"producto_id": p["id"], "nombre": p["nombre"], "cantidad": i.cantidad, "costo_unitario": costo_u})
            monto = round(bruto + d.extra, 2)
            cur = base.ejec("INSERT INTO plata (ts, tipo, categoria, concepto, monto, items) VALUES (?, 'compra', 'Mercadería', ?, ?, ?)",
                            (ts, d.proveedor.strip() or "Compra de mercadería", monto, json.dumps(items)))
            for x in items:
                base.mover_stock(x["producto_id"], x["cantidad"], "Compra", f"Compra {cur.lastrowid}" + (f" · {d.proveedor}" if d.proveedor else ""), ts=ts)
        fondo.add_task(empujar_stock)
        return fila(base.uno("SELECT * FROM plata WHERE id = ?", (cur.lastrowid,)))

    @app.post("/api/plata", dependencies=[Depends(admin)])
    def cargar_plata(d: MovPlata):
        cur = base.ejec("INSERT INTO plata (ts, tipo, categoria, concepto, monto) VALUES (?,?,?,?,?)",
                        (d.fecha_ms or ahora_ms(), d.tipo, d.categoria.strip(), d.concepto.strip(), round(d.monto, 2)))
        return fila(base.uno("SELECT * FROM plata WHERE id = ?", (cur.lastrowid,)))

    @app.get("/api/plata", dependencies=[Depends(admin)])
    def listar_plata(desde: int = 0, hasta: int = 0, limite: int = 300):
        hasta = hasta or ahora_ms() + 86_400_000
        return [fila(m) for m in base.q("SELECT * FROM plata WHERE ts >= ? AND ts < ? ORDER BY ts DESC, id DESC LIMIT ?",
                                        (desde, hasta, min(max(limite, 1), 2000)))]

    @app.post("/api/plata/{mov_id}/anular", dependencies=[Depends(admin)])
    def anular_plata(mov_id: int, fondo: BackgroundTasks):
        with base.transaccion():
            m = base.uno("SELECT * FROM plata WHERE id = ?", (mov_id,))
            if not m:
                raise HTTPException(404, "No existe ese movimiento")
            if m["anulado"]:
                raise HTTPException(409, "Ya estaba anulado")
            base.ejec("UPDATE plata SET anulado = 1 WHERE id = ?", (mov_id,))
            for x in json.loads(m["items"]):  # una compra anulada saca la mercadería que había sumado
                base.mover_stock(x["producto_id"], -x["cantidad"], "Anulación", f"Anula compra {mov_id}")
        fondo.add_task(empujar_stock)
        return fila(base.uno("SELECT * FROM plata WHERE id = ?", (mov_id,)))

    # ---------- balance
    @app.get("/api/balance", dependencies=[Depends(admin)])
    def balance(desde: int, hasta: int, tz: int = -180):
        """Resumen del negocio entre desde y hasta (ms). Incluye el período anterior del mismo largo para comparar."""
        if hasta <= desde:
            raise HTTPException(422, "El período está al revés")

        def numeros(d, h):
            vs = [fila(v) for v in base.q("SELECT * FROM ventas WHERE anulada = 0 AND ts >= ? AND ts < ?", (d, h))]
            pl = base.q("SELECT tipo, categoria, SUM(monto) m FROM plata WHERE anulado = 0 AND ts >= ? AND ts < ? GROUP BY tipo, categoria", (d, h))
            ingresos = sum(v["total"] for v in vs)
            costo = sum(v["costo"] for v in vs)
            envios = sum(v["envio_costo"] for v in vs)
            bruta = ingresos - costo - envios
            gastos = sum(f["m"] for f in pl if f["tipo"] == "gasto")
            return vs, pl, {
                "ventas": len(vs), "ingresos": round(ingresos, 2), "costo_mercaderia": round(costo, 2),
                "envios": round(envios, 2), "descuentos": round(sum(v["descuento"] for v in vs), 2),
                "ganancia_bruta": round(bruta, 2), "gastos": round(gastos, 2), "ganancia_neta": round(bruta - gastos, 2),
                "margen": round(bruta / ingresos * 100, 1) if ingresos else 0,
                "ticket_promedio": round(ingresos / len(vs), 2) if vs else 0,
                "compras": round(sum(f["m"] for f in pl if f["tipo"] == "compra"), 2),
                "inversiones": round(sum(f["m"] for f in pl if f["tipo"] == "inversion"), 2),
            }

        vs, pl, r = numeros(desde, hasta)
        _, _, anterior = numeros(desde - (hasta - desde), desde)

        def agrupar(clave):
            g = {}
            for v in vs:
                k = clave(v)
                x = g.setdefault(k, {"nombre": k, "ventas": 0, "ingresos": 0.0, "ganancia": 0.0})
                x["ventas"] += 1
                x["ingresos"] += v["total"]
                x["ganancia"] += v["total"] - v["costo"] - v["envio_costo"]
            return sorted(({**x, "ingresos": round(x["ingresos"], 2), "ganancia": round(x["ganancia"], 2)} for x in g.values()),
                          key=lambda x: -x["ingresos"])

        productos, sin_costo = {}, 0
        for v in vs:
            for i in v["items"]:
                k = i.get("producto_id") or ("?" + i["nombre"])
                x = productos.setdefault(k, {"nombre": i["nombre"], "unidades": 0, "ingresos": 0.0, "ganancia": 0.0})
                x["unidades"] += i["cantidad"]
                x["ingresos"] += i["precio"] * i["cantidad"]
                x["ganancia"] += (i["precio"] - (i.get("costo") or 0)) * i["cantidad"]
                sin_costo += 1 if not i.get("costo") else 0
        dias = {}
        for v in vs:
            k = inicio_dia(v["ts"], tz)
            x = dias.setdefault(k, {"dia": k, "ventas": 0, "ingresos": 0.0, "ganancia": 0.0})
            x["ventas"] += 1
            x["ingresos"] += v["total"]
            x["ganancia"] += v["total"] - v["costo"] - v["envio_costo"]
        stock = base.uno("SELECT COALESCE(SUM(MAX(stock,0) * costo),0) costo, COALESCE(SUM(MAX(stock,0) * precio),0) venta, "
                         "COALESCE(SUM(MAX(stock,0)),0) unidades, SUM(CASE WHEN stock <= 0 THEN 1 ELSE 0 END) sin_stock, "
                         "SUM(CASE WHEN costo = 0 THEN 1 ELSE 0 END) sin_costo FROM productos WHERE activo = 1 AND controla_stock = 1")
        return {
            **r,
            "anterior": anterior,
            "por_origen": agrupar(lambda v: "Tienda web" if v["origen"] == "web" else "Por fuera (mostrador)"),
            "por_medio_pago": agrupar(lambda v: v["medio_pago"] or "Sin dato"),
            "productos": sorted(({**x, "ingresos": round(x["ingresos"], 2), "ganancia": round(x["ganancia"], 2)} for x in productos.values()),
                                key=lambda x: -x["ingresos"])[:15],
            "gastos_por_categoria": sorted(({"categoria": f["categoria"] or "Otros", "monto": round(f["m"], 2)} for f in pl if f["tipo"] == "gasto"),
                                           key=lambda x: -x["monto"]),
            "por_dia": [{**x, "ingresos": round(x["ingresos"], 2), "ganancia": round(x["ganancia"], 2)} for _, x in sorted(dias.items())],
            "lineas_sin_costo": sin_costo,
            "stock": {"valor_costo": round(stock["costo"], 2), "valor_venta": round(stock["venta"], 2), "unidades": stock["unidades"],
                      "sin_stock": stock["sin_stock"] or 0, "productos_sin_costo": stock["sin_costo"] or 0},
        }

    # ---------- clientes
    def clave_cliente(c: dict) -> str:
        if c.get("email"):
            return "e:" + c["email"].strip().lower()
        tel = "".join(ch for ch in str(c.get("telefono") or "") if ch.isdigit())
        if len(tel) >= 6:
            return "t:" + tel[-8:]  # últimos 8 dígitos: así +54 9 261 ... y 261 ... son el mismo
        if (c.get("nombre") or "").strip() and c["nombre"] != "(borrado a pedido)":
            return "n:" + c["nombre"].strip().lower()
        return ""

    @app.get("/api/clientes", dependencies=[Depends(admin)])
    def clientes():
        """Clientes armados a partir de las ventas (web y por fuera), con sus compras."""
        g = {}
        for v in base.q("SELECT * FROM ventas WHERE anulada = 0 ORDER BY ts"):
            v = fila(v)
            c = v["cliente"] or {}
            k = clave_cliente(c)
            if not k:
                continue
            x = g.setdefault(k, {"clave": k, "nombre": "", "email": "", "telefono": "", "documento": "", "ciudad": "", "provincia": "",
                                 "compras": 0, "total": 0.0, "unidades": 0, "primera": v["ts"], "ultima": v["ts"], "origenes": set(), "ventas": []})
            for campo in ("nombre", "email", "telefono", "documento", "ciudad", "provincia"):
                if c.get(campo):
                    x[campo] = c[campo]  # queda el dato más nuevo
            x["compras"] += 1
            x["total"] += v["total"]
            x["unidades"] += sum(i["cantidad"] for i in v["items"])
            x["ultima"] = v["ts"]
            x["origenes"].add(v["origen"])
            x["ventas"].append({"id": v["id"], "ts": v["ts"], "total": v["total"], "origen": v["origen"], "numero": v["pedido_numero"],
                                "items": [f"{i['cantidad']:g} × {i['nombre']}" for i in v["items"]]})
        out = []
        for x in g.values():
            x["total"] = round(x["total"], 2)
            x["promedio"] = round(x["total"] / x["compras"], 2)
            x["origenes"] = sorted(x["origenes"])
            x["ventas"].reverse()
            out.append(x)
        return sorted(out, key=lambda x: -x["ultima"])

    # Funciones expuestas para los tests y para el hilo de reintentos.
    app.state.procesar_aviso = procesar_aviso
    app.state.empujar_stock = empujar_stock
    app.state.registrar_venta_web = registrar_venta_web
    app.state.reintentar_pendientes = reintentar_pendientes

    if tareas_de_fondo:
        parar = threading.Event()

        def ciclo():
            while not parar.wait(SEGUNDOS_REINTENTO):
                try:
                    reintentar_pendientes()
                except Exception as e:  # pragma: no cover
                    log.exception("Fallo en reintentos: %s", e)

        @app.on_event("startup")
        def arrancar():  # pragma: no cover
            threading.Thread(target=ciclo, daemon=True, name="reintentos").start()

        @app.on_event("shutdown")
        def apagar():  # pragma: no cover
            parar.set()

    return app


app = crear_app() if os.environ.get("ADMIN_TOKEN") else None  # uvicorn app:app
