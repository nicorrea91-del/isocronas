/**
 * Carga del grafo de calles y de la grilla de candidatos.
 *
 * Los binarios que genera el pipeline son simplemente typed arrays pegados uno
 * tras otro, alineados a 4 bytes. Acá se crean vistas sobre el ArrayBuffer sin
 * copiar nada, así que cargar 200.000 nodos es instantáneo.
 */

const CONSTRUCTOR_DTYPE = {
  int32: Int32Array,
  uint16: Uint16Array,
  uint8: Uint8Array,
};

function vista(buffer, secciones, nombre) {
  const seccion = secciones[nombre];
  if (!seccion) throw new Error(`Falta la sección "${nombre}" en el binario`);
  const Ctor = CONSTRUCTOR_DTYPE[seccion.dtype];
  if (!Ctor) throw new Error(`Tipo de dato desconocido: ${seccion.dtype}`);
  return new Ctor(buffer, seccion.offset, seccion.bytes / Ctor.BYTES_PER_ELEMENT);
}

function aGrados(enteros) {
  const salida = new Float64Array(enteros.length);
  for (let i = 0; i < enteros.length; i++) salida[i] = enteros[i] / 1e7;
  return salida;
}

/**
 * Invierte el grafo: para cada nodo, qué aristas LLEGAN a él.
 *
 * Hace falta porque el tiempo de casa->trabajo no es el mismo que de
 * trabajo->casa (calles de un sentido, y la punta de la mañana carga el
 * sentido entrante). Con el grafo invertido, un solo Dijkstra desde el trabajo
 * entrega el tiempo desde CUALQUIER punto de la ciudad hasta el trabajo.
 *
 * Se construye acá en vez de venir en el binario: son 200 ms una sola vez
 * contra un 40% más de descarga cada vez.
 */
function invertir(fwd, n) {
  const m = fwd.targets.length;
  const offsets = new Int32Array(n + 1);
  for (let e = 0; e < m; e++) offsets[fwd.targets[e] + 1]++;
  for (let i = 0; i < n; i++) offsets[i + 1] += offsets[i];

  const cursor = offsets.slice(0, n);
  const targets = new Int32Array(m);
  const baseDs = new Uint16Array(m);
  const largoM = new Uint16Array(m);
  const groups = new Uint8Array(m);

  for (let u = 0; u < n; u++) {
    const fin = fwd.offsets[u + 1];
    for (let e = fwd.offsets[u]; e < fin; e++) {
      const pos = cursor[fwd.targets[e]]++;
      targets[pos] = u;
      baseDs[pos] = fwd.baseDs[e];
      largoM[pos] = fwd.largoM[e];
      groups[pos] = fwd.groups[e];
    }
  }

  return { offsets, targets, baseDs, largoM, groups };
}

export class Grafo {
  constructor(meta, buffer) {
    const secciones = meta.grafo.secciones;
    this.n = meta.grafo.nodos;
    this.lat = aGrados(vista(buffer, secciones, 'lat'));
    this.lon = aGrados(vista(buffer, secciones, 'lon'));
    this.fwd = {
      offsets: vista(buffer, secciones, 'offsets'),
      targets: vista(buffer, secciones, 'targets'),
      baseDs: vista(buffer, secciones, 'base_ds'),
      largoM: vista(buffer, secciones, 'largo_m'),
      groups: vista(buffer, secciones, 'grupos'),
    };
    this.rev = invertir(this.fwd, this.n);
    this.m = this.fwd.targets.length;
    this.bbox = meta.grafo.bbox;
    this.gruposVia = meta.grupos_via;
  }

  /**
   * Nodo más cercano a una coordenada. Fuerza bruta sobre typed arrays:
   * ~200.000 comparaciones, un par de milisegundos. Un índice espacial sería
   * más código para ganancia imperceptible a escala humana.
   */
  nodoMasCercano(lat, lon) {
    const cosLat = Math.cos((lat * Math.PI) / 180);
    let mejor = -1;
    let mejorD2 = Infinity;
    for (let i = 0; i < this.n; i++) {
      const dLat = this.lat[i] - lat;
      const dLon = (this.lon[i] - lon) * cosLat;
      const d2 = dLat * dLat + dLon * dLon;
      if (d2 < mejorD2) {
        mejorD2 = d2;
        mejor = i;
      }
    }
    return mejor;
  }

  dentroDelBbox(lat, lon) {
    return (
      lat >= this.bbox.lat_min &&
      lat <= this.bbox.lat_max &&
      lon >= this.bbox.lon_min &&
      lon <= this.bbox.lon_max
    );
  }
}

export class Grilla {
  constructor(meta, buffer) {
    const info = meta.grilla;
    const secciones = info.secciones;
    this.n = info.celdas;
    this.lat = aGrados(vista(buffer, secciones, 'lat'));
    this.lon = aGrados(vista(buffer, secciones, 'lon'));
    this.nodo = vista(buffer, secciones, 'nodo');
    this.comuna = vista(buffer, secciones, 'comuna');
    this.ix = vista(buffer, secciones, 'ix');
    this.iy = vista(buffer, secciones, 'iy');
    this.nx = info.nx;
    this.ny = info.ny;
    this.bbox = info.bbox;
    this.pasoLat = info.paso_lat;
    this.pasoLon = info.paso_lon;
    this.espaciadoM = info.espaciado_m;
    this.comunas = info.comunas;
  }

  nombreComuna(indiceCelda) {
    return this.comunas[this.comuna[indiceCelda]] || '(fuera)';
  }
}

/**
 * Alinea los factores de tráfico al orden de grupos que usa el grafo.
 * Los dos archivos los genera el mismo pipeline, pero emparejar por nombre en
 * vez de por posición evita un error silencioso y carísimo de depurar si algún
 * día cambia el orden en config.py.
 */
export function alinearFactores(trafico, gruposVia) {
  const franjas = {};
  for (const franja of trafico.franjas) {
    const factores = new Float64Array(gruposVia.length);
    for (let i = 0; i < gruposVia.length; i++) {
      const posicion = trafico.grupos.indexOf(gruposVia[i]);
      if (posicion < 0) throw new Error(`traffic.json no tiene el grupo "${gruposVia[i]}"`);
      factores[i] = franja.g[posicion];
    }
    franjas[franja.id] = {
      id: franja.id,
      label: franja.label,
      horaReferencia: franja.hora_referencia,
      t0: franja.t0_s,
      factores,
    };
  }
  return franjas;
}

export async function cargarDatos(base = 'data/') {
  const traer = async (ruta, tipo) => {
    const respuesta = await fetch(base + ruta, { cache: 'no-cache' });
    if (!respuesta.ok) {
      throw new Error(
        `No se pudo cargar ${ruta} (${respuesta.status}). ` +
          `¿Corriste el pipeline? Ver README.`
      );
    }
    return tipo === 'json' ? respuesta.json() : respuesta.arrayBuffer();
  };

  const [meta, trafico] = await Promise.all([
    traer('graph_meta.json', 'json'),
    traer('traffic.json', 'json'),
  ]);
  const [bufferGrafo, bufferGrilla] = await Promise.all([
    traer(meta.grafo.archivo, 'bin'),
    traer(meta.grilla.archivo, 'bin'),
  ]);

  const grafo = new Grafo(meta, bufferGrafo);
  const grilla = new Grilla(meta, bufferGrilla);
  const franjas = alinearFactores(trafico, grafo.gruposVia);

  return { meta, grafo, grilla, trafico, franjas };
}
