// Demo del panel para GitHub Pages: no hay servidor, así que este archivo hace de "servidor de mentira".
// Atiende los mismos pedidos que conector/app.py (/api/...) con productos y ventas inventados, guardados en memoria.
// Lo que se toca en la demo (ventas, ajustes, gastos) se pierde al recargar la página.
(() => {
  // ---------- azar repetible: la demo muestra siempre lo mismo
  let semilla = 261;
  const rnd = () => { semilla = (semilla * 16807) % 2147483647; return (semilla - 1) / 2147483646; };
  const elegir = a => a[Math.floor(rnd() * a.length)];
  const DIA = 864e5, ahora = Date.now();
  const r2 = n => Math.round(n * 100) / 100;

  // ---------- productos (multirubro: motos, ventiladores, celulares, hogar)
  const BASE = [
    ['Ventilador de pie 18"', 'VENT-18', 45900, 27000, 14, 4], ['Ventilador de mano recargable', 'VENT-MANO', 6900, 2900, 38, 10],
    ['Turbo ventilador de mesa', 'VENT-TURBO', 32500, 19800, 6, 3], ['Casco rebatible negro mate', 'CASCO-REB', 68000, 41000, 5, 2],
    ['Soporte de celular para moto', 'MOTO-SOP', 9800, 3900, 25, 6], ['Cubre puños térmicos', 'MOTO-PUÑO', 14500, 7200, 11, 4],
    ['Funda silicona iPhone 13', 'FUN-IP13', 5900, 1600, 42, 10], ['Funda antigolpe Samsung A54', 'FUN-A54', 6500, 1900, 3, 8],
    ['Cargador rápido 20W', 'CARG-20W', 11900, 5200, 30, 8], ['Auriculares inalámbricos', 'AURI-BT', 18900, 8700, 17, 5],
    ['Lámpara LED de escritorio', 'LAMP-LED', 15800, 7400, 9, 3], ['Organizador de cocina giratorio', 'COC-GIRA', 12400, 5100, 0, 3],
    ['Set de 6 contenedores herméticos', 'COC-HERM', 16900, 7800, 21, 5], ['Tira LED RGB 5 m', 'LED-RGB5', 13500, 5600, 19, 5],
    ['Mini aspiradora para auto', 'AUTO-ASP', 21900, 10900, 8, 3], ['Sticker reflectivo', 'STK-REF', 1500, 0, 0, 0],
  ];
  const productos = BASE.map(([nombre, sku, precio, costo, stock, minimo], i) => ({
    id: i + 1, nombre, sku, codigo_barras: '', precio, costo, stock, minimo, controla_stock: sku === 'STK-REF' ? 0 : 1,
    tn_producto_id: 1000 + i, tn_variante_id: sku === 'LAMP-LED' ? null : 5000 + i, sincronizado: 1, activo: 1, modificado: ahora,
  }));
  const prod = id => productos.find(p => p.id === id);

  // ---------- clientes de ejemplo
  const CLIENTES = [
    ['Ana Gómez', '2615551234', 'Mendoza', 'ana.gomez'], ['Juan Pérez', '2614449876', 'Godoy Cruz', 'juanpe'],
    ['Lucía Funes', '2616667788', 'Luján de Cuyo', 'lufunes'], ['Martín Sosa', '2615123344', 'Guaymallén', 'msosa'],
    ['Carla Ríos', '2614012233', 'Las Heras', 'carla.rios'], ['Diego Ortiz', '2616334455', 'Maipú', 'dortiz'],
    ['Sofía Navarro', '2615778899', 'Mendoza', 'sofinavarro'], ['Pablo Aguirre', '2617001122', 'San Rafael', 'paguirre'],
    ['Valentina Díaz', '2614556677', 'Godoy Cruz', 'vale.diaz'], ['Nicolás Molina', '2616998877', 'Guaymallén', 'nicomolina'],
  ];
  const CALLES = ['San Martín', 'Belgrano', 'Las Heras', 'Colón', 'Godoy Cruz', 'Perú', 'Paso de los Andes'];

  // ---------- ventas de los últimos 75 días
  const ventas = [], movimientos = [], plata = [], avisos = [];
  let numeroWeb = 1180;
  const mover = (p, cant, tipo, detalle, ts, venta_id) => {
    if (!p || !p.controla_stock) return;
    p.stock = r2(p.stock + cant);
    movimientos.push({ id: movimientos.length + 1, ts, producto_id: p.id, cantidad: cant, saldo: p.stock, tipo, detalle, venta_id: venta_id || null });
  };
  function nuevaVenta(ts, origen) {
    const items = [], n = rnd() < .7 ? 1 : 2;
    for (let k = 0; k < n; k++) {
      const p = elegir(productos.filter(x => x.controla_stock));
      if (items.some(i => i.producto_id === p.id)) continue;
      items.push({ producto_id: p.id, nombre: p.nombre, sku: p.sku, cantidad: rnd() < .85 ? 1 : 2, precio: p.precio, costo: p.costo, sin_producto: false, variante: '', imagen: '' });
    }
    const subtotal = items.reduce((a, i) => a + i.precio * i.cantidad, 0);
    const costo = items.reduce((a, i) => a + i.costo * i.cantidad, 0);
    const [nombre, tel, ciudad, mail] = elegir(CLIENTES);
    const id = ventas.length + 1;
    if (origen === 'web') {
      const envio = rnd() < .45 ? 0 : 3500, numero = String(++numeroWeb);
      const det = {
        creado_ts: ts - 6 * 6e4, pagado_ts: ts, enviado_ts: ts < ahora - 3 * DIA ? ts + DIA : null, cancelado_ts: null,
        estado: 'open', estado_pago: 'paid', estado_envio: ts < ahora - 3 * DIA ? (ts < ahora - 6 * DIA ? 'delivered' : 'shipped') : 'unpacked',
        origen: 'store', moneda: 'ARS',
        envio: { metodo: envio ? 'Envío a domicilio (48 hs)' : 'Retiro en el local', transportista: '', retiro: !envio, cobrado: envio, costo_tienda: envio ? 3200 : 0,
          seguimiento: '', seguimiento_url: '', destinatario: nombre, telefono: tel, direccion: envio ? `${elegir(CALLES)} ${100 + Math.floor(rnd() * 2400)}` : '',
          localidad: '', ciudad: envio ? ciudad : '', provincia: envio ? 'Mendoza' : '', cp: envio ? '5500' : '' },
        pago: { plataforma: 'Mercado Pago', metodo: elegir(['credit_card', 'credit_card', 'debit_card', 'account_money']), tarjeta: elegir(['visa', 'mastercard', 'naranja']), cuotas: elegir([1, 1, 3, 3, 6]), operacion: String(90000000000 + Math.floor(rnd() * 9e9)) },
        descuentos: { total: 0, cupon: 0, medio_pago: 0, cupones: [] },
        facturacion: { nombre, documento: '', direccion: '', ciudad: '', provincia: '', cp: '' }, nota_cliente: '', nota_tienda: '',
      };
      ventas.push({ id, ts, origen, pedido_tn_id: 900000 + id, pedido_numero: numero, items, subtotal, envio, descuento: 0, total: subtotal + envio,
        costo: r2(costo), medio_pago: 'Mercado Pago', cliente: { nombre, email: mail + '@ejemplo.com', telefono: tel, documento: '', ciudad, provincia: 'Mendoza' },
        revisar: 0, anulada: 0, anulada_ts: null, detalle: det, envio_costo: det.envio.costo_tienda });
      items.forEach(i => mover(prod(i.producto_id), -i.cantidad, 'Venta web', 'Pedido #' + numero, ts, id));
    } else {
      const conCliente = rnd() < .6, descuento = rnd() < .2 ? 1000 : 0;
      ventas.push({ id, ts, origen, pedido_tn_id: null, pedido_numero: '', items, subtotal, envio: 0, descuento, total: Math.max(subtotal - descuento, 0),
        costo: r2(costo), medio_pago: elegir(['Efectivo', 'Transferencia', 'Transferencia', 'Mercado Pago']),
        cliente: conCliente ? { nombre, telefono: tel, email: '' } : { nombre: '', telefono: '', email: '' },
        revisar: 0, anulada: 0, anulada_ts: null, detalle: { nota_tienda: elegir(['', '', 'Pidió por WhatsApp', 'Retiró en mano']) }, envio_costo: 0 });
      items.forEach(i => mover(prod(i.producto_id), -i.cantidad, 'Venta mostrador', 'Venta ' + id, ts, id));
    }
  }
  // Se arma del pasado al presente; el stock inicial se sube primero para que al final quede el de la tabla.
  const stockFinal = productos.map(p => p.stock);
  const plan = [];
  for (let d = 75; d >= 0; d--) {
    const cuantas = d === 0 ? 4 : Math.floor(rnd() * 4) + (d % 7 === 5 || d % 7 === 6 ? 2 : 1);
    for (let k = 0; k < cuantas; k++) {
      const inicio = new Date(ahora - d * DIA); inicio.setHours(9, 0, 0, 0);
      let ts = +inicio + rnd() * 12 * 36e5;
      if (ts > ahora) ts = ahora - (k + 1) * 47 * 6e4;
      plan.push([ts, rnd() < .62 ? 'web' : 'mostrador']);
    }
  }
  plan.sort((a, b) => a[0] - b[0]);
  // Primero una pasada "en seco" para saber cuánto stock hace falta al principio.
  const guardado = semilla;
  plan.forEach(([ts, o]) => nuevaVenta(ts, o));
  const vendido = {}; movimientos.forEach(m => { vendido[m.producto_id] = (vendido[m.producto_id] || 0) - m.cantidad; });
  ventas.length = 0; movimientos.length = 0; semilla = guardado; numeroWeb = 1180;
  productos.forEach((p, i) => {
    p.stock = stockFinal[i] + (vendido[p.id] || 0);
    if (p.controla_stock) movimientos.push({ id: movimientos.length + 1, ts: ahora - 80 * DIA, producto_id: p.id, cantidad: p.stock, saldo: p.stock, tipo: 'Inicial', detalle: 'Stock traído de Tiendanube', venta_id: null });
  });
  plan.forEach(([ts, o]) => nuevaVenta(ts, o));
  // Una venta por fuera anulada, para que se vea cómo queda.
  const anu = ventas.filter(v => v.origen === 'mostrador').slice(-5)[0];
  if (anu) { anu.anulada = 1; anu.anulada_ts = anu.ts + 36e5; anu.items.forEach(i => mover(prod(i.producto_id), i.cantidad, 'Anulación', 'Anula venta ' + anu.id, anu.anulada_ts)); }
  productos.forEach((p, i) => { if (p.controla_stock) p.stock = stockFinal[i]; });
  productos.find(p => p.sku === 'FUN-A54').sincronizado = 0;

  // ---------- gastos, inversiones y compras
  const mes = n => { const d = new Date(); d.setDate(1); d.setHours(12, 0, 0, 0); d.setMonth(d.getMonth() - n); return +d; };
  const gasto = (ts, tipo, categoria, concepto, monto, items) => plata.push({ id: plata.length + 1, ts, tipo, categoria, concepto, monto, items: items || [], anulado: 0 });
  [2, 1, 0].forEach(n => {
    gasto(mes(n) + 2 * DIA, 'gasto', 'Plan Tiendanube', 'Plan Esencial', 27000);
    gasto(mes(n) + 4 * DIA, 'gasto', 'Publicidad', 'Campaña de Instagram', 45000 + n * 5000);
    gasto(mes(n) + 9 * DIA, 'gasto', 'Comisión Mercado Pago', 'Comisiones del mes', 38000 - n * 4000);
    gasto(mes(n) + 12 * DIA, 'gasto', 'Embalaje', 'Cajas y cinta', 9800);
  });
  gasto(mes(1) + 6 * DIA, 'inversion', 'Equipamiento', 'Estantería para depósito', 85000);
  gasto(mes(1) + 15 * DIA, 'compra', 'Mercadería', 'Importadora Once', 540000,
    [{ producto_id: 1, nombre: productos[0].nombre, cantidad: 12, costo_unitario: 27000 }, { producto_id: 9, nombre: productos[8].nombre, cantidad: 40, costo_unitario: 5400 }]);
  gasto(mes(0) + 3 * DIA, 'compra', 'Mercadería', 'Proveedor fundas', 96000,
    [{ producto_id: 7, nombre: productos[6].nombre, cantidad: 60, costo_unitario: 1600 }]);
  plata.forEach(m => { if (m.ts > ahora) m.ts = ahora - 2 * 36e5; });

  // ---------- avisos de Tiendanube
  ventas.filter(v => v.origen === 'web').slice(-6).forEach(v => avisos.push({ id: avisos.length + 1, recibido_ts: v.ts + 2000, evento: 'order/paid', recurso_id: v.pedido_tn_id, cuerpo: '{}', estado: 'hecho', intentos: 1, ultimo_error: '', procesado_ts: v.ts + 3000 }));
  avisos.reverse();

  // ---------- lo mismo que calcula conector/app.py
  const inicioDia = (ms, tz) => Math.floor((ms + tz * 6e4) / DIA) * DIA - tz * 6e4;
  const activas = () => ventas.filter(v => !v.anulada);
  const totales = desde => { const vs = activas().filter(v => v.ts >= desde); return { n: vs.length, total: vs.reduce((a, v) => a + v.total, 0), ganancia: vs.reduce((a, v) => a + v.total - v.envio_costo - v.costo, 0) }; };
  const copia = x => JSON.parse(JSON.stringify(x));
  let ultimaTraida = ahora - 4 * 6e4;

  function estado(tz) {
    const local = new Date(ahora + tz * 6e4), hoy = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate()) - tz * 6e4;
    const mesIni = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), 1) - tz * 6e4;
    return {
      conectada: true, tienda_id: '4242123', configurada: true, url_avisos: '', ultima_importacion: ahora - 2 * DIA, ultima_traida: ultimaTraida,
      error_traer: '', con_avisos: false, traer_cada_min: 2, hoy: totales(hoy), mes: totales(mesIni),
      stock_bajo: productos.filter(p => p.activo && p.controla_stock && p.stock <= p.minimo).length,
      sin_sincronizar: productos.filter(p => !p.sincronizado && p.tn_variante_id).length,
      ventas_revisar: activas().filter(v => v.revisar).length, avisos_error: 0,
    };
  }
  function balance(desde, hasta, tz) {
    const numeros = (d, h) => {
      const vs = activas().filter(v => v.ts >= d && v.ts < h), pl = plata.filter(m => !m.anulado && m.ts >= d && m.ts < h);
      const ingresos = vs.reduce((a, v) => a + v.total, 0), costo = vs.reduce((a, v) => a + v.costo, 0), envios = vs.reduce((a, v) => a + v.envio_costo, 0);
      const bruta = ingresos - costo - envios, sum = t => pl.filter(m => m.tipo === t).reduce((a, m) => a + m.monto, 0), gastos = sum('gasto');
      return [vs, pl, { ventas: vs.length, ingresos: r2(ingresos), costo_mercaderia: r2(costo), envios: r2(envios), descuentos: r2(vs.reduce((a, v) => a + v.descuento, 0)),
        ganancia_bruta: r2(bruta), gastos: r2(gastos), ganancia_neta: r2(bruta - gastos), margen: ingresos ? Math.round(bruta / ingresos * 1000) / 10 : 0,
        ticket_promedio: vs.length ? r2(ingresos / vs.length) : 0, compras: r2(sum('compra')), inversiones: r2(sum('inversion')) }];
    };
    const [vs, pl, r] = numeros(desde, hasta), anterior = numeros(desde - (hasta - desde), desde)[2];
    const agrupar = clave => { const g = {}; vs.forEach(v => { const k = clave(v), x = g[k] = g[k] || { nombre: k, ventas: 0, ingresos: 0, ganancia: 0 }; x.ventas++; x.ingresos += v.total; x.ganancia += v.total - v.costo - v.envio_costo; }); return Object.values(g).sort((a, b) => b.ingresos - a.ingresos); };
    const prods = {}; let sinCosto = 0;
    vs.forEach(v => v.items.forEach(i => { const k = i.producto_id || '?' + i.nombre, x = prods[k] = prods[k] || { nombre: i.nombre, unidades: 0, ingresos: 0, ganancia: 0 }; x.unidades += i.cantidad; x.ingresos += i.precio * i.cantidad; x.ganancia += (i.precio - (i.costo || 0)) * i.cantidad; if (!i.costo) sinCosto++; }));
    const dias = {}; vs.forEach(v => { const k = inicioDia(v.ts, tz), x = dias[k] = dias[k] || { dia: k, ventas: 0, ingresos: 0, ganancia: 0 }; x.ventas++; x.ingresos += v.total; x.ganancia += v.total - v.costo - v.envio_costo; });
    const cs = productos.filter(p => p.activo && p.controla_stock);
    const g = {}; pl.filter(m => m.tipo === 'gasto').forEach(m => { g[m.categoria || 'Otros'] = (g[m.categoria || 'Otros'] || 0) + m.monto; });
    return { ...r, anterior, por_origen: agrupar(v => v.origen === 'web' ? 'Tienda web' : 'Por fuera (mostrador)'), por_medio_pago: agrupar(v => v.medio_pago || 'Sin dato'),
      productos: Object.values(prods).sort((a, b) => b.ingresos - a.ingresos).slice(0, 15),
      gastos_por_categoria: Object.entries(g).map(([categoria, monto]) => ({ categoria, monto })).sort((a, b) => b.monto - a.monto),
      por_dia: Object.values(dias).sort((a, b) => a.dia - b.dia), lineas_sin_costo: sinCosto,
      stock: { valor_costo: r2(cs.reduce((a, p) => a + Math.max(p.stock, 0) * p.costo, 0)), valor_venta: r2(cs.reduce((a, p) => a + Math.max(p.stock, 0) * p.precio, 0)),
        unidades: cs.reduce((a, p) => a + Math.max(p.stock, 0), 0), sin_stock: cs.filter(p => p.stock <= 0).length, productos_sin_costo: cs.filter(p => !p.costo).length } };
  }
  function clientes() {
    const g = {};
    activas().slice().sort((a, b) => a.ts - b.ts).forEach(v => {
      const c = v.cliente || {}, tel = String(c.telefono || '').replace(/\D/g, '');
      const k = c.email ? 'e:' + c.email.toLowerCase() : tel.length >= 6 ? 't:' + tel.slice(-8) : (c.nombre || '').trim() ? 'n:' + c.nombre.trim().toLowerCase() : '';
      if (!k) return;
      const x = g[k] = g[k] || { clave: k, nombre: '', email: '', telefono: '', documento: '', ciudad: '', provincia: '', compras: 0, total: 0, unidades: 0, primera: v.ts, ultima: v.ts, origenes: new Set(), ventas: [] };
      ['nombre', 'email', 'telefono', 'documento', 'ciudad', 'provincia'].forEach(f => { if (c[f]) x[f] = c[f]; });
      x.compras++; x.total += v.total; x.unidades += v.items.reduce((a, i) => a + i.cantidad, 0); x.ultima = v.ts; x.origenes.add(v.origen);
      x.ventas.push({ id: v.id, ts: v.ts, total: v.total, origen: v.origen, numero: v.pedido_numero, items: v.items.map(i => `${i.cantidad} × ${i.nombre}`) });
    });
    return Object.values(g).map(x => ({ ...x, total: r2(x.total), promedio: r2(x.total / x.compras), origenes: [...x.origenes].sort(), ventas: x.ventas.reverse() })).sort((a, b) => b.ultima - a.ultima);
  }

  // ---------- el "servidor"
  const error = (status, detail) => ({ status, body: { detail } });
  function atender(metodo, url, cuerpo) {
    const u = new URL(url, location.href), ruta = u.pathname.slice(u.pathname.indexOf('/api/')), q = k => u.searchParams.get(k), num = k => Number(q(k) || 0);
    const tz = q('tz') !== null ? num('tz') : -180;
    let m;
    if (metodo === 'GET') {
      if (ruta === '/api/estado') return estado(tz);
      if (ruta === '/api/productos') return productos.filter(p => p.activo).sort((a, b) => a.nombre.localeCompare(b.nombre));
      if ((m = ruta.match(/^\/api\/productos\/(\d+)\/movimientos$/))) return movimientos.filter(x => x.producto_id === +m[1]).sort((a, b) => b.id - a.id).slice(0, num('limite') || 100);
      if (ruta === '/api/ventas') return ventas.slice().sort((a, b) => b.ts - a.ts || b.id - a.id).slice(0, num('limite') || 200);
      if ((m = ruta.match(/^\/api\/ventas\/(\d+)\/pedido$/))) { const v = ventas.find(x => x.id === +m[1]); return v ? { id: v.pedido_tn_id, number: v.pedido_numero, total: v.total, nota: 'Pedido de ejemplo (demo)' } : error(404, 'No existe esa venta'); }
      if (ruta === '/api/balance') return num('hasta') <= num('desde') ? error(422, 'El período está al revés') : balance(num('desde'), num('hasta'), tz);
      if (ruta === '/api/plata') { const d = num('desde'), h = num('hasta') || ahora + DIA; return plata.filter(x => x.ts >= d && x.ts < h).sort((a, b) => b.ts - a.ts || b.id - a.id); }
      if (ruta === '/api/clientes') return clientes();
      if (ruta === '/api/avisos') return avisos.slice(0, num('limite') || 100);
      if (ruta === '/api/diferencias') return productos.filter(p => !p.sincronizado && p.tn_variante_id).map(p => ({ producto_id: p.id, nombre: p.nombre, sku: p.sku, aca: p.stock, tiendanube: p.stock + 2 }));
    }
    if (metodo === 'PATCH' && (m = ruta.match(/^\/api\/productos\/(\d+)$/))) {
      const p = prod(+m[1]); if (!p) return error(404, 'No existe ese producto');
      if (cuerpo.costo != null) p.costo = cuerpo.costo; if (cuerpo.minimo != null) p.minimo = cuerpo.minimo; return p;
    }
    if (metodo === 'POST') {
      if ((m = ruta.match(/^\/api\/productos\/(\d+)\/ajuste$/))) {
        const p = prod(+m[1]); if (!p) return error(404, 'No existe ese producto');
        const dif = r2(cuerpo.stock_real - p.stock); if (dif) mover(p, dif, 'Ajuste', `${cuerpo.motivo} (había ${p.stock}, se contaron ${cuerpo.stock_real})`, Date.now()); return p;
      }
      if (ruta === '/api/ventas') {
        const items = cuerpo.items.map(i => { const p = prod(i.producto_id); return { producto_id: p.id, nombre: p.nombre, sku: p.sku, cantidad: i.cantidad, precio: i.precio ?? p.precio, costo: p.costo, sin_producto: false }; });
        const subtotal = r2(items.reduce((a, i) => a + i.precio * i.cantidad, 0)), id = ventas.length + 1;
        const v = { id, ts: Date.now(), origen: 'mostrador', pedido_tn_id: null, pedido_numero: '', items, subtotal, envio: 0, descuento: cuerpo.descuento || 0,
          total: r2(Math.max(subtotal - (cuerpo.descuento || 0), 0)), costo: r2(items.reduce((a, i) => a + i.costo * i.cantidad, 0)), medio_pago: cuerpo.medio_pago,
          cliente: { nombre: cuerpo.cliente || '', telefono: cuerpo.telefono || '', email: (cuerpo.email || '').toLowerCase() }, revisar: 0, anulada: 0, anulada_ts: null,
          detalle: { nota_tienda: cuerpo.nota || '' }, envio_costo: 0 };
        ventas.push(v); items.forEach(i => mover(prod(i.producto_id), -i.cantidad, 'Venta mostrador', 'Venta ' + id, v.ts, id)); return v;
      }
      if ((m = ruta.match(/^\/api\/ventas\/(\d+)\/anular$/))) {
        const v = ventas.find(x => x.id === +m[1]); if (!v) return error(404, 'No existe esa venta');
        if (v.origen === 'web') return error(422, 'Las ventas web se cancelan desde Tiendanube; el conector se entera solo');
        if (v.anulada) return error(409, 'Esa venta ya estaba anulada');
        v.anulada = 1; v.anulada_ts = Date.now(); v.items.forEach(i => mover(prod(i.producto_id), i.cantidad, 'Anulación', 'Anula venta ' + v.id, v.anulada_ts)); return v;
      }
      if (ruta === '/api/plata') { gasto(cuerpo.fecha_ms || Date.now(), cuerpo.tipo, cuerpo.categoria, cuerpo.concepto, r2(cuerpo.monto)); return plata[plata.length - 1]; }
      if (ruta === '/api/compras') {
        const bruto = cuerpo.items.reduce((a, i) => a + i.cantidad * i.costo_unitario, 0), ts = cuerpo.fecha_ms || Date.now();
        const items = cuerpo.items.map(i => {
          const p = prod(i.producto_id), extra = bruto ? cuerpo.extra * (i.cantidad * i.costo_unitario) / bruto / i.cantidad : 0, cu = r2(i.costo_unitario + extra);
          const previo = p.controla_stock ? Math.max(p.stock, 0) : 0; p.costo = previo ? r2((previo * p.costo + i.cantidad * cu) / (previo + i.cantidad)) : cu;
          return { producto_id: p.id, nombre: p.nombre, cantidad: i.cantidad, costo_unitario: cu };
        });
        gasto(ts, 'compra', 'Mercadería', cuerpo.proveedor || 'Compra de mercadería', r2(bruto + cuerpo.extra), items);
        const c = plata[plata.length - 1]; items.forEach(i => mover(prod(i.producto_id), i.cantidad, 'Compra', 'Compra ' + c.id, ts)); return c;
      }
      if ((m = ruta.match(/^\/api\/plata\/(\d+)\/anular$/))) {
        const x = plata.find(y => y.id === +m[1]); if (!x) return error(404, 'No existe ese movimiento'); if (x.anulado) return error(409, 'Ya estaba anulado');
        x.anulado = 1; x.items.forEach(i => mover(prod(i.producto_id), -i.cantidad, 'Anulación', 'Anula compra ' + x.id, Date.now())); return x;
      }
      if (ruta === '/api/simular-pedido') { nuevaVenta(Date.now(), 'web'); return { venta_id: ventas.length, repetido: false, revisar: false, historica: false }; }
      if (ruta === '/api/traer-pedidos') { ultimaTraida = Date.now(); return { ok: true, nuevas: 0, anuladas: 0 }; }
      if (ruta === '/api/sincronizar') { const n = productos.filter(p => !p.sincronizado).length; productos.forEach(p => { p.sincronizado = 1; }); return { enviados: n, errores: 0 }; }
      if (ruta === '/api/productos/importar') return { nuevos: 0, actualizados: productos.length };
      if (ruta === '/api/revisar-conexion') return { avisos: [], enviados: 0 };
      if (ruta === '/api/avisos/' + (ruta.split('/')[3]) + '/reintentar') return { estado: 'hecho' };
      if (ruta.startsWith('/api/instalar')) return error(422, 'Esto es una demo: no se conecta a ninguna tienda de verdad.');
    }
    return error(404, 'Esto no existe en la demo');
  }

  // El panel usa fetch('/api/...'): acá se contesta sin salir a internet.
  const fetchReal = window.fetch.bind(window);
  window.fetch = async (url, op = {}) => {
    if (!String(url).includes('/api/')) return fetchReal(url, op);
    const r = atender((op.method || 'GET').toUpperCase(), String(url), op.body ? JSON.parse(op.body) : null);
    const conError = r && r.status && r.body && r.body.detail !== undefined && Object.keys(r).length === 2;
    return new Response(JSON.stringify(copia(conError ? r.body : r)), { status: conError ? r.status : 200, headers: { 'Content-Type': 'application/json' } });
  };

  // En la demo no hay clave: se entra directo.
  try { localStorage.setItem('panel-token', 'demo'); } catch (e) {}

  // Cartel arriba para que quede claro que los datos son inventados.
  document.addEventListener('DOMContentLoaded', () => {
    const st = document.createElement('style');
    st.textContent = '#demoCartel{background:#f2c35b;color:#1b1f2a;font:600 13px/1.4 system-ui,sans-serif;text-align:center;padding:7px 16px}'
      + '#demoCartel a{color:inherit}'
      + '@media (min-width:861px){#demoCartel{position:fixed;top:0;left:0;right:0;z-index:50;height:32px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}body{padding-top:32px}#lado{top:32px!important}}';
    document.head.appendChild(st);
    const c = document.createElement('div');
    c.id = 'demoCartel'; c.setAttribute('role', 'note');
    c.innerHTML = 'Demo con datos inventados: nada de esto es real y lo que cargues se borra al recargar. <a href="../">Ver la tienda</a>';
    document.body.prepend(c);
  });
})();
