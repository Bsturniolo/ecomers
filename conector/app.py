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
from datetime import datetime
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
                items.append({"producto_id": p["id"] if p else None, "nombre": str(it.get("name") or (p and p["nombre"]) or "?"),
                              "sku": str(it.get("sku") or ""), "cantidad": cant, "precio": num(it.get("price")),
                              "costo": p["costo"] if p else 0, "sin_producto": p is None})
                costo += (p["costo"] if p else 0) * cant
                revisar = revisar or p is None
            c = pedido.get("customer") or {}
            cliente = {"nombre": str(c.get("name") or pedido.get("contact_name") or ""),
                       "email": str(c.get("email") or pedido.get("contact_email") or ""),
                       "telefono": str(c.get("phone") or pedido.get("contact_phone") or ""),
                       "tn_cliente_id": c.get("id")}
            ts = iso_a_ms(pedido.get("paid_at")) or iso_a_ms(pedido.get("created_at")) or ahora_ms()
            cur = base.ejec(
                "INSERT INTO ventas (ts, origen, pedido_tn_id, pedido_numero, items, subtotal, envio, descuento, total, costo, "
                "medio_pago, cliente, revisar) VALUES (?, 'web', ?,?,?,?,?,?,?,?,?,?,?)",
                (ts, pid, str(pedido.get("number") or pid), json.dumps(items), num(pedido.get("subtotal")),
                 num(pedido.get("shipping_cost_customer")), num(pedido.get("discount")), num(pedido.get("total")),
                 round(costo, 2), medio or str(pedido.get("gateway_name") or pedido.get("gateway") or "Tienda web"),
                 json.dumps(cliente), int(revisar)))
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

    @app.get("/api/estado", dependencies=[Depends(admin)])
    def estado():
        hoy = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        desde_hoy = int(hoy.timestamp() * 1000)
        desde_mes = int(hoy.replace(day=1).timestamp() * 1000)

        def totales(desde):
            f = base.uno("SELECT COUNT(*) n, COALESCE(SUM(total),0) total, COALESCE(SUM(total - envio - costo),0) ganancia "
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
            cur = base.ejec("INSERT INTO ventas (ts, origen, items, subtotal, descuento, total, costo, medio_pago, cliente) "
                            "VALUES (?, 'mostrador', ?,?,?,?,?,?,?)",
                            (ahora_ms(), json.dumps(items), subtotal, d.descuento, total, round(costo, 2), d.medio_pago,
                             json.dumps({"nombre": d.cliente})))
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
        pedido = {"id": -n, "number": f"PRUEBA-{n}", "payment_status": "paid", "products": prods, "subtotal": subtotal,
                  "shipping_cost_customer": 0, "discount": 0, "total": subtotal, "customer": {"name": "Cliente de prueba"}}
        return registrar_venta_web(pedido, medio="Simulado")

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
