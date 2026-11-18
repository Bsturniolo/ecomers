# Ecomers · Conector Tiendanube

Sistema chico para la tienda de la clienta: **cuando entra una venta pagada en Tiendanube, se registra sola, descuenta el stock y calcula la ganancia.** Las ventas de mostrador y los ajustes de stock hechos en el panel se mandan a Tiendanube, para no vender lo que no hay.

Toma lo que servía de [MILENIUM-2](https://github.com/Bsturniolo/MILENIUM-2) (servidor FastAPI + SQLite, firma de los avisos, pedidos que no se cuentan dos veces, kardex de stock, deploy con Docker y HTTPS) y lo adapta a cómo funciona Tiendanube.

## Qué hace

| Parte | Qué hace |
| --- | --- |
| Instalación | La clienta acepta la app en Tiendanube y el conector queda conectado. Pide los avisos de venta y trae todos los productos (cada variante con su SKU) |
| Ventas web | Tiendanube avisa cuando un pedido queda **pagado**. El conector busca el pedido, registra la venta (productos, envío, descuento, medio de pago, cliente) y descuenta el stock. Si el pedido se **cancela**, anula la venta y devuelve el stock |
| Sin duplicados | Si Tiendanube manda el mismo aviso varias veces, la venta cuenta una sola vez |
| Si algo falla | El aviso queda guardado y se reintenta solo cada minuto. Después de 12 intentos queda marcado en el panel |
| Productos sin SKU | Si se vende algo que no se reconoce, la venta entra igual pero queda marcada **para revisar** |
| Mostrador | Ventas en el local desde el panel: descuenta stock acá y en Tiendanube |
| Stock | Ajustes con motivo, kardex por producto, aviso de stock bajo, comparar el stock con Tiendanube y corregirlo con un botón |
| Datos de cada venta web | Horario del pedido y del pago, productos y variantes, envío (método, dirección, seguimiento, estado), pago (tarjeta, cuotas, N° de operación), cupones, DNI y notas. El pedido completo de Tiendanube queda guardado |
| Balances | Por período: ventas, costo de la mercadería, envíos, gastos, ganancia bruta y neta, margen, comparación con el período anterior, ventas por día, por canal y por medio de pago, productos más vendidos y valor del stock. Se cargan gastos, inversiones y compras de mercadería (suman stock y recalculan el costo) |
| Clientes | Lista armada sola con las ventas web y las de afuera: contacto, ciudad, compras, total gastado, quién repite. Se descarga como planilla |
| Panel | `/panel`: inicio, ventas, productos, vender, balances, clientes, conexión. Funciona en el celular |

Lo que **no** hace (todavía): facturación electrónica, varias sucursales de stock, compras a proveedores.

## Cómo dejarlo andando

Paso a paso en **[docs/PASOS.md](docs/PASOS.md)**: crear la app en Tiendanube Partners, subir el servidor y conectar la tienda.

## Probarlo en la compu

```bash
pip install -r requirements-dev.txt
python -m pytest -q tests              # pruebas con una Tiendanube falsa
cd conector && ADMIN_TOKEN=una-clave-de-prueba-larga uvicorn app:app --reload
# abrir http://localhost:8000/panel  →  Conexión → "Simular venta web"
```

## Estructura

| Archivo | Para qué |
| --- | --- |
| `conector/app.py` | Servidor: avisos de Tiendanube, ventas, stock y la API del panel |
| `conector/tiendanube.py` | Todo lo que habla con Tiendanube (instalación, firma, API) |
| `conector/base.py` | Base de datos (un archivo SQLite) |
| `conector/panel.html` | El panel (HTML y JavaScript, sin dependencias) |
| `tests/` | Pruebas de punta a punta con una Tiendanube falsa |
| `Dockerfile` · `deploy/` | Todo para subirlo a un servidor con HTTPS |
