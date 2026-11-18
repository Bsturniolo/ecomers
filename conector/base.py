"""Base de datos (SQLite) del conector.

Un archivo, sin servidor aparte. Guarda:
- productos: cada variante de Tiendanube es un producto (con su SKU, costo y stock).
- ventas: las de la tienda web y las de mostrador, con su detalle.
- movimientos: cada cambio de stock con su motivo (kardex).
- avisos: los avisos que manda Tiendanube, para procesarlos una sola vez y reintentar si algo falla.
- config: la conexión con la tienda (número de tienda y llave de acceso).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

ESQUEMA = """
CREATE TABLE IF NOT EXISTS config (
  clave TEXT PRIMARY KEY,
  valor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS productos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nombre TEXT NOT NULL,
  sku TEXT NOT NULL DEFAULT '',
  codigo_barras TEXT NOT NULL DEFAULT '',
  precio REAL NOT NULL DEFAULT 0,
  costo REAL NOT NULL DEFAULT 0,
  stock REAL NOT NULL DEFAULT 0,
  minimo REAL NOT NULL DEFAULT 0,
  controla_stock INTEGER NOT NULL DEFAULT 1,
  tn_producto_id INTEGER,
  tn_variante_id INTEGER UNIQUE,
  sincronizado INTEGER NOT NULL DEFAULT 1,   -- 0 = falta mandarle el stock a Tiendanube
  activo INTEGER NOT NULL DEFAULT 1,
  modificado INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS productos_sku ON productos (sku);
CREATE TABLE IF NOT EXISTS ventas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  origen TEXT NOT NULL CHECK (origen IN ('web','mostrador')),
  pedido_tn_id INTEGER UNIQUE,              -- id del pedido en Tiendanube (solo ventas web)
  pedido_numero TEXT NOT NULL DEFAULT '',   -- número que ve la clienta en Tiendanube (#123)
  items TEXT NOT NULL,                      -- JSON
  subtotal REAL NOT NULL DEFAULT 0,
  envio REAL NOT NULL DEFAULT 0,
  descuento REAL NOT NULL DEFAULT 0,
  total REAL NOT NULL,
  costo REAL NOT NULL DEFAULT 0,
  medio_pago TEXT NOT NULL DEFAULT '',
  cliente TEXT NOT NULL DEFAULT '{}',       -- JSON
  revisar INTEGER NOT NULL DEFAULT 0,       -- 1 = tiene productos que no se encontraron
  anulada INTEGER NOT NULL DEFAULT 0,
  anulada_ts INTEGER
);
CREATE TABLE IF NOT EXISTS movimientos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  producto_id INTEGER NOT NULL REFERENCES productos(id),
  cantidad REAL NOT NULL,
  saldo REAL NOT NULL,
  tipo TEXT NOT NULL,
  detalle TEXT NOT NULL DEFAULT '',
  venta_id INTEGER
);
CREATE TABLE IF NOT EXISTS avisos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recibido_ts INTEGER NOT NULL,
  evento TEXT NOT NULL,
  recurso_id INTEGER,
  cuerpo TEXT NOT NULL,
  estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','hecho','error','ignorado')),
  intentos INTEGER NOT NULL DEFAULT 0,
  ultimo_error TEXT NOT NULL DEFAULT '',
  procesado_ts INTEGER
);
CREATE INDEX IF NOT EXISTS avisos_estado ON avisos (estado);
CREATE TABLE IF NOT EXISTS plata (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  tipo TEXT NOT NULL CHECK (tipo IN ('compra','gasto','inversion')),
  categoria TEXT NOT NULL DEFAULT '',
  concepto TEXT NOT NULL DEFAULT '',
  monto REAL NOT NULL,
  items TEXT NOT NULL DEFAULT '[]',       -- JSON: productos de una compra de mercadería
  anulado INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ventas_ts ON ventas (ts);
CREATE INDEX IF NOT EXISTS plata_ts ON plata (ts);
"""

# Columnas agregadas después de la primera versión: se suman solas a una base vieja.
COLUMNAS_NUEVAS = {
    "ventas": {
        "detalle": "TEXT NOT NULL DEFAULT '{}'",    # JSON: horarios, envío, pago, cupones, notas
        "pedido_tn": "TEXT NOT NULL DEFAULT ''",    # JSON: el pedido completo tal como lo mandó Tiendanube
        "envio_costo": "REAL NOT NULL DEFAULT 0",   # lo que paga la tienda por el envío
    },
}


def ahora_ms() -> int:
    return int(time.time() * 1000)


class Base:
    def __init__(self, ruta: str):
        self.con = sqlite3.connect(ruta, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.execute("PRAGMA journal_mode = WAL")
        self.con.executescript(ESQUEMA)
        for tabla, cols in COLUMNAS_NUEVAS.items():
            hay = {f["name"] for f in self.con.execute(f"PRAGMA table_info({tabla})")}
            for col, tipo in cols.items():
                if col not in hay:
                    self.con.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
        # Un solo lock para todo: las operaciones son cortas y así nunca se pisan dos cambios de stock.
        self.lock = threading.RLock()

    # ---------- consultas simples
    def q(self, sql: str, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def uno(self, sql: str, args=()):
        filas = self.q(sql, args)
        return filas[0] if filas else None

    def ejec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            return self.con.execute(sql, args)

    def transaccion(self):
        """Uso: with base.transaccion(): ...  (todo o nada)."""
        return _Transaccion(self)

    # ---------- configuración
    def get(self, clave: str, defecto: str = "") -> str:
        f = self.uno("SELECT valor FROM config WHERE clave = ?", (clave,))
        return f["valor"] if f else defecto

    def set(self, clave: str, valor: str) -> None:
        self.ejec("INSERT INTO config (clave, valor) VALUES (?, ?) ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor",
                  (clave, valor))

    def borrar(self, clave: str) -> None:
        self.ejec("DELETE FROM config WHERE clave = ?", (clave,))

    # ---------- stock
    def mover_stock(self, producto_id: int, cantidad: float, tipo: str, detalle: str = "",
                    venta_id: int | None = None, avisar_tienda: bool = True, ts: int | None = None) -> float | None:
        """Suma (o resta, si es negativo) stock y deja la línea en el kardex.

        avisar_tienda=False cuando el cambio ya pasó en Tiendanube (una venta web): así no se descuenta dos veces allá.
        Devuelve el saldo, o None si el producto no controla stock.
        """
        with self.lock:
            p = self.con.execute("SELECT * FROM productos WHERE id = ?", (producto_id,)).fetchone()
            if not p or not p["controla_stock"] or not cantidad:
                return None
            saldo = round(p["stock"] + cantidad, 3)
            self.con.execute("UPDATE productos SET stock = ?, modificado = ?, sincronizado = ? WHERE id = ?",
                             (saldo, ahora_ms(), 0 if avisar_tienda and p["tn_variante_id"] else p["sincronizado"], producto_id))
            self.con.execute("INSERT INTO movimientos (ts, producto_id, cantidad, saldo, tipo, detalle, venta_id) VALUES (?,?,?,?,?,?,?)",
                             (ts or ahora_ms(), producto_id, cantidad, saldo, tipo, detalle, venta_id))
            return saldo


class _Transaccion:
    def __init__(self, base: Base):
        self.base = base

    def __enter__(self):
        self.base.lock.acquire()
        self.base.con.execute("BEGIN IMMEDIATE")
        return self.base

    def __exit__(self, tipo, *_):
        try:
            self.base.con.execute("ROLLBACK" if tipo else "COMMIT")
        finally:
            self.base.lock.release()
        return False


def fila(f) -> dict:
    """Fila de SQLite a dict, abriendo los campos JSON."""
    d = dict(f)
    d.pop("pedido_tn", None)  # el pedido crudo es pesado: se pide aparte
    for k in ("items", "cliente", "detalle"):
        if k in d and isinstance(d[k], str):
            d[k] = json.loads(d[k])
    return d
