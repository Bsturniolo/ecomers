# Pasos para dejarlo andando

Hay dos formas de usarlo:

| | **Sin servidor** (gratis) | **Con servidor** |
| --- | --- | --- |
| Dónde corre | En una compu (la de la clienta o la tuya) | En un servidor en internet, siempre prendido |
| Cuándo entran las ventas web | Cada vez que se abre la app, y cada 2 minutos mientras está abierta. Si estuvo apagada días, al abrirla se pone al día sola | Al instante, a cualquier hora |
| Costo | Nada | El servidor (unos pocos dólares por mes) |
| Desde dónde se ve el panel | Solo desde esa compu | Desde cualquier compu o celular |

Para arrancar, **sin servidor** alcanza. Si después hace falta, se pasa al servidor y no se pierde nada.

## Sin servidor (en una compu)

1. **Crear la app en Tiendanube Partners** (paso 1 de abajo). Como URL de redirección poné `http://localhost:8000/tiendanube/instalada`. Si el portal no acepta `localhost`, poné cualquier página tuya: después se copia el código a mano. Las URLs de privacidad pueden apuntar a esa misma página.
2. **Instalar Python** en esa compu (3.12 o 3.13). En Windows, tildá "Add python.exe to PATH".
3. **Bajar el código** de GitHub (botón verde Code → Download ZIP, y descomprimirlo; o `git clone`).
4. **Arrancar:** doble clic en `iniciar.bat` (Windows) o `./iniciar.sh` (Linux/Mac). La primera vez tarda un poco porque instala lo necesario. Se abre el panel en el navegador y en la ventana negra aparece la **clave**.
5. Cerrá esa ventana, abrí `datos/config.env`, completá `TN_APP_ID`, `TN_APP_SECRETO` y `TN_CONTACTO`, y volvé a arrancar.
6. En el panel: **Conexión → Conectar la tienda**. Se abre Tiendanube: aceptá la app. Si vuelve sola diciendo "Listo", ya está. Si muestra un error, copiá la dirección completa de esa pestaña (tiene `code=`) y pegala en el panel. El código dura 5 minutos.

Al conectar, trae los productos y las ventas de los últimos 30 días. Esas ventas viejas se cargan para los reportes pero no tocan el stock, porque Tiendanube ya las tenía descontadas. Para traer más atrás: Conexión → "Traer ventas desde una fecha".

Todo queda en la carpeta `datos` (la clave, la configuración y la base). **Hacé copia de esa carpeta de vez en cuando**, por ejemplo en un pendrive o en Drive: si se rompe la compu, ahí están las ventas, costos y gastos. Las ventas web igual siguen en Tiendanube.

Para actualizar cuando haya cambios: bajar el código nuevo (o `git pull`) **sin tocar la carpeta `datos`**, y volver a arrancar.

## Con servidor

Son 4 partes. Las marcadas con 👤 necesitan a la clienta o una cuenta.

## 1. Crear la app en Tiendanube Partners 👤

1. Entrar a [partners.tiendanube.com](https://partners.tiendanube.com) y crear una cuenta de partner (es gratis).
2. Crear una app nueva para **una sola tienda** (no hace falta publicarla en la tienda de apps).
3. Completar:
   - **URL de redirección** (a dónde vuelve después de instalar): `https://TU-DOMINIO/tiendanube/instalada`
   - **Permisos**: leer pedidos (`read_orders`) y leer y escribir productos (`read_products`, `write_products`).
   - **Avisos de privacidad** (los pide Tiendanube a toda app):
     - Borrado de la tienda (store redact): `https://TU-DOMINIO/tiendanube/privacidad/store-redact`
     - Borrado de cliente (customers redact): `https://TU-DOMINIO/tiendanube/privacidad/customers-redact`
     - Pedido de datos de cliente (customers data request): `https://TU-DOMINIO/tiendanube/privacidad/customers-data-request`
4. Anotar el **App ID** y el **Client Secret** de la app. El secreto no se comparte con nadie.

> Los nombres exactos de los botones del portal pueden cambiar. Lo importante es la URL de redirección, los permisos y las 3 URLs de privacidad.

## 2. Subir el servidor

Hace falta un servidor con Docker (cualquier VPS chico alcanza) y un dominio o subdominio que apunte a su IP (por ejemplo `panel.latienda.com.ar`).

```bash
git clone https://github.com/Bsturniolo/ecomers.git
cd ecomers/deploy
cp .env.ejemplo .env
nano .env        # completar DOMINIO, URL_PUBLICA, ADMIN_TOKEN, TN_APP_ID, TN_APP_SECRETO, TN_CONTACTO
docker compose up -d --build
```

- `ADMIN_TOKEN` es la clave del panel. Generarla con `openssl rand -hex 24`.
- `TN_TIENDA_ID` (opcional) es el número de la tienda de la clienta. Si se pone, la app solo se puede instalar en esa tienda.
- El certificado HTTPS lo saca solo Caddy. Tiendanube **solo avisa a direcciones con https**.
- Los datos quedan en un volumen de Docker (`datos`). Para una copia de seguridad: `docker compose cp conector:/datos/conector.db ./copia.db`.

Revisar que ande: abrir `https://TU-DOMINIO/salud` tiene que mostrar `{"ok":true}`.

## 3. Conectar la tienda 👤

1. Abrir `https://TU-DOMINIO/panel` y entrar con el `ADMIN_TOKEN`.
2. **Conexión → Conectar la tienda.** Lleva a Tiendanube: hay que estar logueado con la cuenta de la clienta (o que ella lo haga) y aceptar.
3. Vuelve al panel solo. En ese momento el conector:
   - pide a Tiendanube los avisos de **pedido pagado**, **pedido cancelado** y **app desinstalada**, y
   - trae todos los productos con su stock.

## 4. Antes de vender de verdad

- **SKU**: cada producto (y cada variante: color, talle) tiene que tener SKU en Tiendanube. Así se reconocen. Los que no tienen aparecen con "sin SKU" en Productos.
- **Costos**: cargarlos en Productos → Editar. Sin costo, la ganancia sale inflada.
- **Prueba**: en Conexión → "Simular venta web" registra una venta de prueba sin tocar Tiendanube. Para probar de punta a punta, hacer una compra real chica en la tienda (o un pedido con pago manual marcado como pagado desde el admin) y ver que aparezca en Ventas en unos segundos.
- **Cancelaciones**: al cancelar un pedido en Tiendanube, dejar tildado que **devuelva el stock**. El conector devuelve el stock de acá; el de Tiendanube lo devuelve Tiendanube.
- **Comparar stock**: en Conexión → "Comparar stock" muestra los productos con stock distinto entre el panel y Tiendanube. "Mandar el stock de acá a Tiendanube" los iguala (manda el panel).

## Cómo se reparte el stock

| Qué pasa | Stock del panel | Stock de Tiendanube |
| --- | --- | --- |
| Venta web pagada | Baja (lo hace el conector) | Baja (lo hace Tiendanube) |
| Pedido cancelado | Vuelve (conector) | Vuelve (Tiendanube) |
| Venta de mostrador | Baja | El conector le manda el número nuevo |
| Ajuste por conteo | Cambia | El conector le manda el número nuevo |
| Cambio hecho a mano en el admin de Tiendanube | No se entera | Cambia |

Por la última fila: **conviene cambiar el stock solo desde el panel**. Si se toca en Tiendanube, usar "Comparar stock" para ver la diferencia.
