/**
 * Interfaz: mapa, puntos, pesos, isócronas y centro de gravedad.
 */

import { cargarDatos } from './graph.js';
import {
  METRICAS,
  Router,
  campoDeCostoSemanal,
  desglosePorPunto,
  rasterizarCampo,
} from './routing.js';
import {
  COLORES_BANDA,
  formatearCosto,
  pintarHeatmapCosto,
  pintarIsocronas,
} from './render.js';

const PALETA = [
  '#2563eb',
  '#db2777',
  '#059669',
  '#d97706',
  '#7c3aed',
  '#0891b2',
  '#dc2626',
  '#65a30d',
];

const PNG_TRANSPARENTE =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';

// Resolución del raster de isócronas. 120 m es más fino que la separación
// típica entre esquinas, así que el borde queda pegado a las calles.
const CELDA_ISO_M = 120;

const PUNTOS_EJEMPLO = [
  { nombre: 'Trabajo', lat: -33.4143, lon: -70.596, viajes: 10, ida: 'punta_am', vuelta: 'punta_pm' },
  { nombre: 'Colegio', lat: -33.362, lon: -70.5185, viajes: 10, ida: 'punta_am', vuelta: 'valle' },
  { nombre: 'Suegros', lat: -33.456, lon: -70.598, viajes: 2, ida: 'valle', vuelta: 'noche' },
];

// ---------------------------------------------------------------------------
// Estado
// ---------------------------------------------------------------------------

const estado = {
  puntos: [],
  metrica: 'tiempo',
  mostrarHeatmap: true,
  isoOrigen: 'optimo',
  isoFranja: 'punta_am',
  isoUmbrales: [15, 25, 40],
  candidato: null,
};

let datos = null;
let router = null;
let mapa = null;
let siguienteId = 1;
let ultimoResultado = null;
let marcadorOptimo = null;
let marcadorCandidato = null;
// Raster de isócrona memoizado: solo se recalcula si cambia el origen, la
// franja o la métrica, no al mover los sliders de minutos.
let cacheRaster = { clave: null, rasterizado: null };

const el = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------------

async function iniciar() {
  try {
    el('cargando-texto').textContent = 'Cargando la red de calles…';
    datos = await cargarDatos();
    router = new Router(datos.grafo);

    el('cargando-texto').textContent = 'Preparando el mapa…';
    construirMapa();
    construirControles();

    if (!cargarDesdeUrl()) {
      for (const ejemplo of PUNTOS_EJEMPLO) agregarPunto(ejemplo, { silencioso: true });
    }

    mostrarPie();
    el('cargando').hidden = true;
    recalcular();
  } catch (error) {
    console.error(error);
    el('cargando-texto').textContent = 'No se pudo cargar el proyecto';
    const caja = el('cargando-error');
    caja.hidden = false;
    caja.textContent =
      `${error.message}\n\n` +
      'Si es la primera vez, hay que generar los datos:\n' +
      '  python pipeline/fetch_osm.py\n' +
      '  python pipeline/build_graph.py\n' +
      '  python pipeline/calibrate.py\n\n' +
      'Y servir la carpeta web/ con un servidor (no abrir el archivo directo):\n' +
      '  python -m http.server 8000 --directory web';
  }
}

function construirMapa() {
  const bbox = datos.grafo.bbox;
  const esquinasBbox = [
    [bbox.lon_min, bbox.lat_max],
    [bbox.lon_max, bbox.lat_max],
    [bbox.lon_max, bbox.lat_min],
    [bbox.lon_min, bbox.lat_min],
  ];
  const oscuro = window.matchMedia('(prefers-color-scheme: dark)').matches;

  mapa = new maplibregl.Map({
    container: 'mapa',
    style: {
      version: 8,
      sources: {
        base: {
          type: 'raster',
          tiles: [
            `https://a.basemaps.cartocdn.com/${oscuro ? 'dark_all' : 'light_all'}/{z}/{x}/{y}{ratio}.png`.replace(
              '{ratio}',
              window.devicePixelRatio > 1 ? '@2x' : ''
            ),
          ],
          tileSize: 256,
          attribution:
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>, &copy; <a href="https://carto.com/attributions">CARTO</a>',
        },
      },
      layers: [{ id: 'base', type: 'raster', source: 'base' }],
    },
    center: [
      (bbox.lon_min + bbox.lon_max) / 2,
      (datos.grilla.bbox.lat_min + datos.grilla.bbox.lat_max) / 2,
    ],
    zoom: 10.5,
    maxBounds: [
      [bbox.lon_min - 0.15, bbox.lat_min - 0.15],
      [bbox.lon_max + 0.15, bbox.lat_max + 0.15],
    ],
  });

  mapa.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');
  mapa.addControl(new maplibregl.ScaleControl({ maxWidth: 110 }), 'bottom-left');

  mapa.on('load', () => {
    // Las dos capas se crean vacías y en este orden para fijar quién va
    // encima: el mapa de calor abajo, las isócronas arriba.
    for (const [id, remuestreo] of [
      // El mapa de calor se ve mejor suavizado; las isócronas necesitan el
      // borde nítido, si no el contorno de las autopistas se difumina.
      ['capa-heatmap', 'linear'],
      ['capa-iso', 'nearest'],
    ]) {
      mapa.addSource(id, {
        type: 'image',
        url: PNG_TRANSPARENTE,
        coordinates: esquinasBbox,
      });
      mapa.addLayer({
        id,
        type: 'raster',
        source: id,
        paint: {
          'raster-opacity': 1,
          'raster-fade-duration': 0,
          'raster-resampling': remuestreo,
        },
      });
    }
    pintar();
  });

  mapa.on('click', (evento) => {
    const { lat, lng } = evento.lngLat;
    if (evento.originalEvent.shiftKey) {
      fijarCandidato(lat, lng);
    } else {
      agregarPunto({ nombre: `Punto ${estado.puntos.length + 1}`, lat, lon: lng, viajes: 4 });
    }
  });
}

// ---------------------------------------------------------------------------
// Controles del panel
// ---------------------------------------------------------------------------

function opcionesDeFranja() {
  return Object.values(datos.franjas).map((franja) => ({ valor: franja.id, texto: franja.label }));
}

function llenarSelect(select, opciones, seleccionado) {
  select.innerHTML = '';
  for (const opcion of opciones) {
    const nodo = document.createElement('option');
    nodo.value = opcion.valor;
    nodo.textContent = opcion.texto;
    select.appendChild(nodo);
  }
  select.value = seleccionado;
}

function construirControles() {
  // Métrica
  const contenedor = el('selector-metrica');
  for (const metrica of Object.values(METRICAS)) {
    const boton = document.createElement('button');
    boton.type = 'button';
    boton.role = 'radio';
    boton.dataset.metrica = metrica.id;
    boton.textContent = metrica.label.replace('Tiempo ', 'T. ').replace('Distancia recorrida', 'Distancia');
    boton.title = metrica.label;
    boton.addEventListener('click', () => {
      estado.metrica = metrica.id;
      cacheRaster.clave = null;
      sincronizarMetrica();
      recalcular();
    });
    contenedor.appendChild(boton);
  }
  sincronizarMetrica();

  // Isócronas
  llenarSelect(el('iso-franja'), opcionesDeFranja(), estado.isoFranja);
  el('iso-franja').addEventListener('change', (evento) => {
    estado.isoFranja = evento.target.value;
    cacheRaster.clave = null;
    programarPintado();
  });

  el('iso-origen').addEventListener('change', (evento) => {
    estado.isoOrigen = evento.target.value;
    cacheRaster.clave = null;
    programarPintado();
  });

  const sliders = el('iso-sliders');
  estado.isoUmbrales.forEach((valor, indice) => {
    const fila = document.createElement('div');
    fila.className = 'iso-slider';
    const color = COLORES_BANDA[Math.min(indice, COLORES_BANDA.length - 1)];
    fila.innerHTML = `
      <label>
        <span><span class="punto-color" style="background:rgb(${color.join(',')})"></span>Isócrona ${indice + 1}</span>
        <output></output>
      </label>
      <input class="slider" type="range" min="0" max="100" step="1" aria-label="Isócrona ${indice + 1}" />`;
    const entrada = fila.querySelector('input');
    const salida = fila.querySelector('output');
    entrada.value = valor;
    const refrescar = () => {
      const numero = Number(entrada.value);
      estado.isoUmbrales[indice] = numero;
      salida.textContent = numero === 0 ? 'apagada' : etiquetaUmbral(numero);
      programarPintado();
    };
    entrada.addEventListener('input', refrescar);
    refrescar();
    sliders.appendChild(fila);
  });

  // Resultado
  el('toggle-heatmap').addEventListener('change', (evento) => {
    estado.mostrarHeatmap = evento.target.checked;
    programarPintado();
  });

  el('btn-centrar').addEventListener('click', () => {
    if (!ultimoResultado) return;
    const celda = ultimoResultado.mejor;
    mapa.flyTo({
      center: [datos.grilla.lon[celda], datos.grilla.lat[celda]],
      zoom: 14,
      duration: 900,
    });
  });

  // Búsqueda
  el('btn-buscar').addEventListener('click', buscarDireccion);
  el('buscar-direccion').addEventListener('keydown', (evento) => {
    if (evento.key === 'Enter') {
      evento.preventDefault();
      buscarDireccion();
    }
  });

  el('btn-compartir').addEventListener('click', compartir);
}

function sincronizarMetrica() {
  for (const boton of el('selector-metrica').querySelectorAll('button')) {
    boton.setAttribute('aria-checked', String(boton.dataset.metrica === estado.metrica));
  }
  const metrica = METRICAS[estado.metrica];
  const explicaciones = {
    tiempo: 'Minutos reales de viaje, con los factores de tráfico calibrados por horario.',
    flujo_libre:
      'Minutos con calles despejadas. Comparado con el anterior, muestra cuánto te cuesta el tráfico.',
    distancia: 'Kilómetros recorridos, sin importar el tiempo. Útil si te preocupa el combustible.',
  };
  el('ayuda-metrica').textContent = explicaciones[metrica.id];
  for (const fila of el('iso-sliders').querySelectorAll('.iso-slider')) {
    fila.querySelector('output').textContent = etiquetaUmbral(
      Number(fila.querySelector('input').value)
    );
  }
}

function etiquetaUmbral(valor) {
  if (valor === 0) return 'apagada';
  return METRICAS[estado.metrica].unidad === 'km' ? `${valor} km` : `${valor} min`;
}

// ---------------------------------------------------------------------------
// Puntos
// ---------------------------------------------------------------------------

function agregarPunto(datosPunto, opciones = {}) {
  const punto = {
    id: siguienteId++,
    nombre: datosPunto.nombre || 'Punto',
    lat: datosPunto.lat,
    lon: datosPunto.lon,
    viajes: datosPunto.viajes ?? 4,
    franjaIda: datos.franjas[datosPunto.ida] ? datosPunto.ida : 'punta_am',
    franjaVuelta: datos.franjas[datosPunto.vuelta] ? datosPunto.vuelta : 'punta_pm',
    color: PALETA[(siguienteId - 2) % PALETA.length],
    nodo: -1,
    marcador: null,
    fila: null,
  };
  punto.nodo = datos.grafo.nodoMasCercano(punto.lat, punto.lon);
  punto.fueraDelArea = !datos.grafo.dentroDelBbox(punto.lat, punto.lon);
  estado.puntos.push(punto);

  crearMarcador(punto);
  renderizarListaPuntos();
  if (!opciones.silencioso) {
    cacheRaster.clave = null;
    recalcular();
  }
  return punto;
}

function quitarPunto(id) {
  const indice = estado.puntos.findIndex((punto) => punto.id === id);
  if (indice < 0) return;
  estado.puntos[indice].marcador?.remove();
  estado.puntos.splice(indice, 1);
  renderizarListaPuntos();
  cacheRaster.clave = null;
  recalcular();
}

function crearMarcador(punto) {
  const nodo = document.createElement('div');
  nodo.className = 'marcador';
  nodo.style.background = punto.color;
  nodo.title = punto.nombre;

  punto.marcador = new maplibregl.Marker({ element: nodo, draggable: true })
    .setLngLat([punto.lon, punto.lat])
    .setPopup(new maplibregl.Popup({ offset: 14, closeButton: false }).setText(punto.nombre))
    .addTo(mapa);

  punto.marcador.on('dragend', () => {
    const { lat, lng } = punto.marcador.getLngLat();
    punto.lat = lat;
    punto.lon = lng;
    punto.nodo = datos.grafo.nodoMasCercano(lat, lng);
    punto.fueraDelArea = !datos.grafo.dentroDelBbox(lat, lng);
    cacheRaster.clave = null;
    recalcular();
  });
}

function renderizarListaPuntos() {
  const lista = el('lista-puntos');
  lista.innerHTML = '';
  const plantilla = el('tpl-punto');

  for (const punto of estado.puntos) {
    const nodo = plantilla.content.cloneNode(true).firstElementChild;
    const buscar = (rol) => nodo.querySelector(`[data-rol="${rol}"]`);

    buscar('color').style.background = punto.color;

    const nombre = buscar('nombre');
    nombre.value = punto.nombre;
    nombre.addEventListener('input', () => {
      punto.nombre = nombre.value;
      punto.marcador.getElement().title = punto.nombre;
      punto.marcador.getPopup().setText(punto.nombre);
      actualizarSelectorOrigen();
      guardarEnUrl();
    });

    buscar('borrar').addEventListener('click', () => quitarPunto(punto.id));

    const viajes = buscar('viajes');
    const viajesValor = buscar('viajes-valor');
    viajes.value = punto.viajes;
    viajesValor.textContent = punto.viajes;
    viajes.addEventListener('input', () => {
      punto.viajes = Number(viajes.value);
      viajesValor.textContent = punto.viajes;
      nodo.classList.toggle('inactivo', punto.viajes === 0);
      programarRecalculo();
    });
    nodo.classList.toggle('inactivo', punto.viajes === 0);

    const opciones = opcionesDeFranja();
    for (const [rol, campo] of [
      ['franja-ida', 'franjaIda'],
      ['franja-vuelta', 'franjaVuelta'],
    ]) {
      const select = buscar(rol);
      llenarSelect(select, opciones, punto[campo]);
      select.addEventListener('change', () => {
        punto[campo] = select.value;
        cacheRaster.clave = null;
        recalcular();
      });
    }

    punto.fila = nodo;
    lista.appendChild(nodo);
  }

  actualizarSelectorOrigen();
}

function actualizarSelectorOrigen() {
  const opciones = [{ valor: 'optimo', texto: 'El óptimo calculado' }];
  if (estado.candidato) opciones.push({ valor: 'candidato', texto: 'El lugar comparado' });
  for (const punto of estado.puntos) {
    opciones.push({ valor: `p${punto.id}`, texto: punto.nombre || 'Punto' });
  }
  const select = el('iso-origen');
  const previo = estado.isoOrigen;
  llenarSelect(select, opciones, previo);
  if (select.value !== previo) {
    estado.isoOrigen = select.value || 'optimo';
    select.value = estado.isoOrigen;
  }
}

// ---------------------------------------------------------------------------
// Cálculo
// ---------------------------------------------------------------------------

let temporizadorRecalculo = null;
function programarRecalculo() {
  clearTimeout(temporizadorRecalculo);
  temporizadorRecalculo = setTimeout(recalcular, 90);
}

let temporizadorPintado = null;
function programarPintado() {
  clearTimeout(temporizadorPintado);
  temporizadorPintado = setTimeout(pintar, 60);
}

function recalcular() {
  const metrica = METRICAS[estado.metrica];
  const activos = estado.puntos.filter((punto) => punto.viajes > 0 && punto.nodo >= 0);

  if (activos.length === 0) {
    ultimoResultado = null;
    el('resultado').innerHTML =
      '<p class="vacio">Agrega al menos un punto con viajes &gt; 0.</p>';
    el('btn-centrar').disabled = true;
    el('leyenda-costo').hidden = true;
    for (const punto of estado.puntos) {
      const detalle = punto.fila?.querySelector('[data-rol="detalle"]');
      if (detalle) detalle.textContent = '';
    }
    pintar();
    guardarEnUrl();
    return;
  }

  const campos = activos.map((punto) =>
    router.camposDePunto(
      punto.nodo,
      metrica,
      datos.franjas[punto.franjaIda],
      datos.franjas[punto.franjaVuelta]
    )
  );
  const pesos = activos.map((punto) => punto.viajes);

  const resultado = campoDeCostoSemanal(datos.grilla, campos, pesos, metrica);
  ultimoResultado = { ...resultado, campos, pesos, activos, metrica };

  mostrarResultado();
  mostrarDetalleDePuntos();
  mostrarCandidato();
  cacheRaster.clave = null; // el óptimo pudo moverse
  pintar();
  guardarEnUrl();
}

function mostrarResultado() {
  const { mejor, mejorCosto, costo, metrica } = ultimoResultado;
  const grilla = datos.grilla;

  if (mejor < 0) {
    el('resultado').innerHTML =
      '<p class="vacio">Ningún lugar de la zona alcanza todos los puntos. Revisa si algún punto quedó fuera del área con datos.</p>';
    el('btn-centrar').disabled = true;
    return;
  }

  const finitos = [];
  for (let c = 0; c < grilla.n; c++) if (isFinite(costo[c])) finitos.push(costo[c]);
  finitos.sort((a, b) => a - b);
  const mediana = finitos[Math.floor(finitos.length / 2)];
  const ahorro = mediana - mejorCosto;

  const viajesTotales = ultimoResultado.pesos.reduce((suma, valor) => suma + valor, 0);
  const unidadPorViaje = mejorCosto / Math.max(1, viajesTotales);

  el('resultado').innerHTML = `
    <p class="titular">${formatearCosto(mejorCosto, metrica)}
      <small>por semana</small></p>
    <p class="lugar"><strong>${grilla.nombreComuna(mejor)}</strong> · ${viajesTotales} viajes semanales ·
      ${formatearCosto(unidadPorViaje, metrica)} por viaje</p>
    <p class="coords">${grilla.lat[mejor].toFixed(5)}, ${grilla.lon[mejor].toFixed(5)}
      &nbsp;·&nbsp; ${formatearCosto(ahorro, metrica)} menos que un lugar mediano de la zona</p>`;

  el('btn-centrar').disabled = false;

  if (marcadorOptimo) marcadorOptimo.remove();
  const nodoMarcador = document.createElement('div');
  nodoMarcador.className = 'marcador-optimo';
  marcadorOptimo = new maplibregl.Marker({ element: nodoMarcador })
    .setLngLat([grilla.lon[mejor], grilla.lat[mejor]])
    .setPopup(
      new maplibregl.Popup({ offset: 16, closeButton: false }).setHTML(
        `<strong>Centro de gravedad</strong><br>${grilla.nombreComuna(mejor)}<br>${formatearCosto(
          mejorCosto,
          metrica
        )} por semana`
      )
    )
    .addTo(mapa);
}

function mostrarDetalleDePuntos() {
  const { mejor, campos, pesos, activos, metrica } = ultimoResultado;
  if (mejor < 0) return;
  const nodoOptimo = datos.grilla.nodo[mejor];
  const desglose = desglosePorPunto(nodoOptimo, campos, pesos, activos, metrica);

  const porId = new Map(desglose.filas.map((fila) => [fila.punto.id, fila]));
  for (const punto of estado.puntos) {
    const detalle = punto.fila?.querySelector('[data-rol="detalle"]');
    if (!detalle) continue;
    if (punto.fueraDelArea) {
      detalle.textContent = 'Fuera del área con datos de calles.';
      continue;
    }
    const fila = porId.get(punto.id);
    detalle.textContent = fila
      ? `Desde el óptimo: ${formatearCosto(fila.ida, metrica)} ida · ` +
        `${formatearCosto(fila.vuelta, metrica)} vuelta · ` +
        `${formatearCosto(fila.semanal, metrica)} a la semana`
      : 'Sin viajes asignados.';
  }
}

// ---------------------------------------------------------------------------
// Candidato para comparar
// ---------------------------------------------------------------------------

function fijarCandidato(lat, lon) {
  const nodo = datos.grafo.nodoMasCercano(lat, lon);
  estado.candidato = { lat, lon, nodo };

  if (marcadorCandidato) marcadorCandidato.remove();
  const elemento = document.createElement('div');
  elemento.className = 'marcador-candidato';
  marcadorCandidato = new maplibregl.Marker({ element: elemento })
    .setLngLat([lon, lat])
    .addTo(mapa);

  actualizarSelectorOrigen();
  mostrarCandidato();
  guardarEnUrl();
}

function mostrarCandidato() {
  const caja = el('candidato');
  if (!estado.candidato || !ultimoResultado) {
    caja.innerHTML = '<p class="vacio">Sin lugar seleccionado.</p>';
    return;
  }

  const { campos, pesos, activos, metrica, mejor, mejorCosto } = ultimoResultado;
  const desglose = desglosePorPunto(estado.candidato.nodo, campos, pesos, activos, metrica);
  const diferencia = desglose.total - mejorCosto;
  const claseDelta = diferencia <= 0.5 ? 'delta-bueno' : 'delta-malo';

  const filas = desglose.filas
    .map(
      (fila) => `
      <tr>
        <td><span class="punto-color" style="background:${fila.punto.color}"></span> ${fila.punto.nombre}</td>
        <td>${formatearCosto(fila.ida, metrica)}</td>
        <td>${formatearCosto(fila.vuelta, metrica)}</td>
        <td>${formatearCosto(fila.semanal, metrica)}</td>
      </tr>`
    )
    .join('');

  caja.innerHTML = `
    <p class="coords">${estado.candidato.lat.toFixed(5)}, ${estado.candidato.lon.toFixed(5)}</p>
    <table>
      <thead>
        <tr><th>Punto</th><th>Ida</th><th>Vuelta</th><th>Semana</th></tr>
      </thead>
      <tbody>${filas}</tbody>
      <tfoot>
        <tr>
          <td>Total</td><td></td><td></td>
          <td>${formatearCosto(desglose.total, metrica)}</td>
        </tr>
        <tr>
          <td>vs. óptimo${mejor >= 0 ? ` (${datos.grilla.nombreComuna(mejor)})` : ''}</td>
          <td></td><td></td>
          <td class="${claseDelta}">${diferencia >= 0 ? '+' : '−'}${formatearCosto(
            Math.abs(diferencia),
            metrica
          )}</td>
        </tr>
      </tfoot>
    </table>`;
}

// ---------------------------------------------------------------------------
// Pintado del mapa
// ---------------------------------------------------------------------------

function nodoOrigenIsocrona() {
  if (estado.isoOrigen === 'optimo') {
    return ultimoResultado && ultimoResultado.mejor >= 0
      ? datos.grilla.nodo[ultimoResultado.mejor]
      : -1;
  }
  if (estado.isoOrigen === 'candidato') {
    return estado.candidato ? estado.candidato.nodo : -1;
  }
  const id = Number(estado.isoOrigen.slice(1));
  const punto = estado.puntos.find((item) => item.id === id);
  return punto ? punto.nodo : -1;
}

function actualizarCapa(id, imagen) {
  if (!mapa.getSource(id)) return;
  if (!imagen) {
    mapa.setLayoutProperty(id, 'visibility', 'none');
    return;
  }
  mapa.getSource(id).updateImage({ url: imagen.url, coordinates: imagen.coordenadas });
  mapa.setLayoutProperty(id, 'visibility', 'visible');
}

function pintar() {
  if (!mapa || !mapa.isStyleLoaded() || !mapa.getSource('capa-iso')) return;
  const metrica = METRICAS[estado.metrica];

  // Mapa de calor del costo semanal
  let imagenHeatmap = null;
  if (estado.mostrarHeatmap && ultimoResultado && ultimoResultado.mejor >= 0) {
    imagenHeatmap = pintarHeatmapCosto(datos.grilla, ultimoResultado.costo);
  }
  actualizarCapa('capa-heatmap', imagenHeatmap);

  const leyenda = el('leyenda-costo');
  if (imagenHeatmap) {
    leyenda.hidden = false;
    el('leyenda-min').textContent = formatearCosto(imagenHeatmap.minimo, metrica);
    el('leyenda-max').textContent = `${formatearCosto(imagenHeatmap.maximo, metrica)} o más`;
  } else {
    leyenda.hidden = true;
  }

  // Isócronas
  const origen = nodoOrigenIsocrona();
  const activas = estado.isoUmbrales.filter((valor) => valor > 0);
  let imagenIso = null;

  if (origen >= 0 && activas.length > 0) {
    const clave = `${origen}|${metrica.id}|${estado.isoFranja}`;
    if (cacheRaster.clave !== clave) {
      const campo = router.campoDesde(origen, metrica, datos.franjas[estado.isoFranja]);
      cacheRaster = {
        clave,
        rasterizado: rasterizarCampo(datos.grafo, campo, datos.grafo.bbox, CELDA_ISO_M, 2),
      };
    }
    imagenIso = pintarIsocronas(cacheRaster.rasterizado, estado.isoUmbrales, metrica.divisor);
    el('iso-estado').textContent =
      `${activas.length} banda${activas.length > 1 ? 's' : ''} · ` +
      `${datos.franjas[estado.isoFranja].label.toLowerCase()}`;
  } else if (origen < 0) {
    el('iso-estado').textContent = 'Elige un origen válido para ver las isócronas.';
  } else {
    el('iso-estado').textContent = 'Todas las bandas están apagadas.';
  }

  actualizarCapa('capa-iso', imagenIso);
}

// ---------------------------------------------------------------------------
// Búsqueda de direcciones (Nominatim)
// ---------------------------------------------------------------------------

async function buscarDireccion() {
  const consulta = el('buscar-direccion').value.trim();
  const lista = el('resultados-busqueda');
  if (consulta.length < 3) {
    lista.hidden = true;
    return;
  }

  const bbox = datos.grafo.bbox;
  const url =
    'https://nominatim.openstreetmap.org/search?format=jsonv2&limit=6&countrycodes=cl' +
    `&q=${encodeURIComponent(consulta)}` +
    `&viewbox=${bbox.lon_min},${bbox.lat_max},${bbox.lon_max},${bbox.lat_min}&bounded=1`;

  lista.hidden = false;
  lista.innerHTML = '<li><button type="button" disabled>Buscando…</button></li>';

  try {
    const respuesta = await fetch(url, { headers: { Accept: 'application/json' } });
    const encontrados = await respuesta.json();
    if (!Array.isArray(encontrados) || encontrados.length === 0) {
      lista.innerHTML = '<li><button type="button" disabled>Sin resultados en la zona</button></li>';
      return;
    }

    lista.innerHTML = '';
    for (const item of encontrados) {
      const fila = document.createElement('li');
      const boton = document.createElement('button');
      boton.type = 'button';
      boton.textContent = item.display_name;
      boton.addEventListener('click', () => {
        const lat = Number(item.lat);
        const lon = Number(item.lon);
        const nombre = item.name || item.display_name.split(',')[0];
        agregarPunto({ nombre, lat, lon, viajes: 4 });
        mapa.flyTo({ center: [lon, lat], zoom: 14 });
        lista.hidden = true;
        el('buscar-direccion').value = '';
      });
      fila.appendChild(boton);
      lista.appendChild(fila);
    }
  } catch (error) {
    lista.innerHTML = '<li><button type="button" disabled>Error al buscar</button></li>';
    console.error(error);
  }
}

// ---------------------------------------------------------------------------
// Compartir por URL
// ---------------------------------------------------------------------------

function guardarEnUrl() {
  const compacto = {
    p: estado.puntos.map((punto) => [
      punto.nombre,
      Number(punto.lat.toFixed(5)),
      Number(punto.lon.toFixed(5)),
      punto.viajes,
      punto.franjaIda,
      punto.franjaVuelta,
    ]),
    m: estado.metrica,
    io: estado.isoOrigen,
    ifr: estado.isoFranja,
    iu: estado.isoUmbrales,
    c: estado.candidato
      ? [Number(estado.candidato.lat.toFixed(5)), Number(estado.candidato.lon.toFixed(5))]
      : null,
  };
  const texto = btoa(unescape(encodeURIComponent(JSON.stringify(compacto))))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
  history.replaceState(null, '', `#e=${texto}`);
}

function cargarDesdeUrl() {
  const coincidencia = location.hash.match(/e=([^&]+)/);
  if (!coincidencia) return false;
  try {
    const base64 = coincidencia[1].replace(/-/g, '+').replace(/_/g, '/');
    const compacto = JSON.parse(decodeURIComponent(escape(atob(base64))));

    estado.metrica = METRICAS[compacto.m] ? compacto.m : 'tiempo';
    estado.isoFranja = datos.franjas[compacto.ifr] ? compacto.ifr : estado.isoFranja;
    if (Array.isArray(compacto.iu) && compacto.iu.length === estado.isoUmbrales.length) {
      estado.isoUmbrales = compacto.iu.map((valor) => Math.max(0, Math.min(100, Number(valor) || 0)));
    }

    for (const fila of compacto.p || []) {
      const [nombre, lat, lon, viajes, ida, vuelta] = fila;
      agregarPunto({ nombre, lat, lon, viajes, ida, vuelta }, { silencioso: true });
    }
    if (Array.isArray(compacto.c)) {
      fijarCandidato(compacto.c[0], compacto.c[1]);
    }
    // El origen de isócrona se aplica al final: los ids de punto ya existen.
    estado.isoOrigen = compacto.io || 'optimo';

    sincronizarMetrica();
    llenarSelect(el('iso-franja'), opcionesDeFranja(), estado.isoFranja);
    for (const [indice, fila] of [...el('iso-sliders').querySelectorAll('.iso-slider')].entries()) {
      const entrada = fila.querySelector('input');
      entrada.value = estado.isoUmbrales[indice];
      fila.querySelector('output').textContent = etiquetaUmbral(estado.isoUmbrales[indice]);
    }
    actualizarSelectorOrigen();
    return (compacto.p || []).length > 0;
  } catch (error) {
    console.warn('No se pudo leer el escenario de la URL:', error);
    return false;
  }
}

async function compartir() {
  guardarEnUrl();
  const boton = el('btn-compartir');
  const original = boton.textContent;
  try {
    await navigator.clipboard.writeText(location.href);
    boton.textContent = '¡Enlace copiado!';
  } catch {
    boton.textContent = 'Copia la URL de la barra';
  }
  setTimeout(() => {
    boton.textContent = original;
  }, 2200);
}

// ---------------------------------------------------------------------------
// Pie con la procedencia de los datos
// ---------------------------------------------------------------------------

function mostrarPie() {
  const fecha = (datos.meta.generado || '').slice(0, 10);
  const trafico = datos.trafico;
  const rmse = trafico.diagnostico?.rmse_global_min;
  const calibracion = rmse
    ? `tráfico calibrado con ${trafico.diagnostico.mediciones.length} mediciones (error típico ${rmse} min)`
    : `tráfico sin calibrar todavía — ${trafico.fuente}`;

  el('pie-datos').textContent =
    `Grafo de ${datos.grafo.n.toLocaleString('es-CL')} esquinas y ` +
    `${datos.grafo.m.toLocaleString('es-CL')} tramos, generado el ${fecha}. ` +
    `Grilla de ${datos.grilla.espaciadoM} m con ${datos.grilla.n.toLocaleString('es-CL')} candidatos. ` +
    `${calibracion[0].toUpperCase()}${calibracion.slice(1)}.`;
}

iniciar();
