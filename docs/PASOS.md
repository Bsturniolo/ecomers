# Pasos para dejarlo andando

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
