/**
 * Motor de ruteo: Dijkstra uno-a-todos, campo de costo semanal y rasterizado
 * para dibujar.
 *
 * La idea central del proyecto está acá. En vez de preguntar "cuánto demoro de
 * mi casa a cada punto", se pregunta desde cada punto hacia toda la ciudad:
 * un Dijkstra desde el colegio sobre el grafo invertido entrega, de una sola
 * pasada, el tiempo desde CUALQUIER esquina de Santiago hasta el colegio.
 *
 * Con eso, un candidato de vivienda no cuesta una consulta: cuesta una lectura
 * de un array. Y sumando los campos ponderados por viajes semanales sale el
 * costo total, cuyo mínimo es el centro de gravedad y cuyas curvas de nivel
 * son el mapa de calor.
 */

/**
 * Heap binario sobre typed arrays. Usa borrado perezoso: si un nodo mejora su
 * distancia se empuja de nuevo y la entrada vieja se descarta al salir. Es más
 * rápido que mantener un heap indexado con decrease-key.
 */
class MinHeap {
  constructor(capacidad) {
    this.claves = new Float64Array(capacidad);
    this.valores = new Int32Array(capacidad);
    this.tamano = 0;
    this.claveUltimo = 0;
  }

  reiniciar() {
    this.tamano = 0;
  }

  _crecer() {
    const clavesNuevas = new Float64Array(this.claves.length * 2);
    const valoresNuevos = new Int32Array(this.valores.length * 2);
    clavesNuevas.set(this.claves);
    valoresNuevos.set(this.valores);
    this.claves = clavesNuevas;
    this.valores = valoresNuevos;
  }

  push(clave, valor) {
    if (this.tamano === this.claves.length) this._crecer();
    let i = this.tamano++;
    this.claves[i] = clave;
    this.valores[i] = valor;
    while (i > 0) {
      const padre = (i - 1) >> 1;
      if (this.claves[padre] <= this.claves[i]) break;
      const ck = this.claves[padre];
      const cv = this.valores[padre];
      this.claves[padre] = this.claves[i];
      this.valores[padre] = this.valores[i];
      this.claves[i] = ck;
      this.valores[i] = cv;
      i = padre;
    }
  }

  pop() {
    this.claveUltimo = this.claves[0];
    const raiz = this.valores[0];
    const ultimo = --this.tamano;
    if (ultimo > 0) {
      this.claves[0] = this.claves[ultimo];
      this.valores[0] = this.valores[ultimo];
      let i = 0;
      for (;;) {
        const izq = 2 * i + 1;
        if (izq >= ultimo) break;
        const der = izq + 1;
        let menor = izq;
        if (der < ultimo && this.claves[der] < this.claves[izq]) menor = der;
        if (this.claves[i] <= this.claves[menor]) break;
        const ck = this.claves[menor];
        const cv = this.valores[menor];
        this.claves[menor] = this.claves[i];
        this.valores[menor] = this.valores[i];
        this.claves[i] = ck;
        this.valores[i] = cv;
        i = menor;
      }
    }
    return raiz;
  }
}

/**
 * Las tres métricas que se pueden minimizar. Todas usan el mismo grafo y el
 * mismo Dijkstra; solo cambia de qué array sale el peso de cada arista.
 */
export const METRICAS = {
  tiempo: {
    id: 'tiempo',
    label: 'Tiempo con tráfico',
    unidad: 'min',
    divisor: 60,
    arreglo: 'baseDs',
    escala: 0.1,
    usaFactores: true,
    usaCostoFijo: true,
  },
  flujo_libre: {
    id: 'flujo_libre',
    label: 'Tiempo sin tráfico',
    unidad: 'min',
    divisor: 60,
    arreglo: 'baseDs',
    escala: 0.1,
    usaFactores: false,
    usaCostoFijo: false,
  },
  distancia: {
    id: 'distancia',
    label: 'Distancia recorrida',
    unidad: 'km',
    divisor: 1000,
    arreglo: 'largoM',
    escala: 1,
    usaFactores: false,
    usaCostoFijo: false,
  },
};

const SIN_FACTORES = new Float64Array(8).fill(1);

export class Router {
  constructor(grafo) {
    this.grafo = grafo;
    this.heap = new MinHeap(Math.max(1024, grafo.m));
    this._cache = new Map();
    this._cacheSimple = new Map();
  }

  /**
   * Costo desde `origen` hasta todos los nodos, recorriendo `csr`.
   * Con csr = grafo.rev y origen = un destino, el resultado se lee al revés:
   * costo desde cualquier nodo hasta ese destino.
   */
  dijkstra(csr, origen, metrica, factores, salida) {
    const n = this.grafo.n;
    const dist = salida || new Float32Array(n);
    dist.fill(Infinity);
    if (origen < 0 || origen >= n) return dist;

    const { offsets, targets, groups } = csr;
    const pesos = csr[metrica.arreglo];
    const escala = metrica.escala;
    const efectivos = metrica.usaFactores ? factores : SIN_FACTORES;
    const heap = this.heap;
    heap.reiniciar();
    dist[origen] = 0;
    heap.push(0, origen);

    while (heap.tamano > 0) {
      const u = heap.pop();
      const d = heap.claveUltimo;
      // Entrada obsoleta del borrado perezoso.
      if (d > dist[u]) continue;
      const fin = offsets[u + 1];
      for (let e = offsets[u]; e < fin; e++) {
        const v = targets[e];
        const nueva = d + pesos[e] * escala * efectivos[groups[e]];
        if (nueva < dist[v]) {
          dist[v] = nueva;
          // Se empuja `dist[v]` y no `nueva`: al guardarlo en el Float32Array
          // el valor se redondea, y si al heap se le diera el float64 sin
          // redondear, la comparación `d > dist[u]` de más arriba daría
          // verdadero para la entrada válida y la descartaría como obsoleta.
          heap.push(dist[v], v);
        }
      }
    }
    return dist;
  }

  /**
   * Campos de costo de un punto: ida (cualquier nodo -> punto, grafo
   * invertido) y vuelta (punto -> cualquier nodo, grafo normal), cada uno con
   * la franja horaria que corresponda.
   *
   * Se memoiza por (nodo, métrica, franja de ida, franja de vuelta): mover el
   * slider de viajes semanales no recalcula nada, solo recombina.
   */
  camposDePunto(nodo, metrica, franjaIda, franjaVuelta) {
    const clave = `${nodo}|${metrica.id}|${franjaIda.id}|${franjaVuelta.id}`;
    const enCache = this._cache.get(clave);
    if (enCache) return enCache;

    const ida = this.dijkstra(this.grafo.rev, nodo, metrica, franjaIda.factores);
    const vuelta = this.dijkstra(this.grafo.fwd, nodo, metrica, franjaVuelta.factores);
    const resultado = {
      ida,
      vuelta,
      t0Ida: metrica.usaCostoFijo ? franjaIda.t0 : 0,
      t0Vuelta: metrica.usaCostoFijo ? franjaVuelta.t0 : 0,
    };

    // Cada campo son 2 x 4 bytes x nodos (~1,6 MB con 200k nodos). Se guardan
    // hasta 24 combinaciones y luego se bota la más antigua.
    if (this._cache.size >= 24) {
      this._cache.delete(this._cache.keys().next().value);
    }
    this._cache.set(clave, resultado);
    return resultado;
  }

  /**
   * Campo simple hacia adelante: costo desde `nodo` hasta cualquier lugar.
   * Es lo que dibuja las isócronas ("hasta dónde llego en 20 minutos").
   */
  campoDesde(nodo, metrica, franja) {
    const clave = `fwd|${nodo}|${metrica.id}|${franja.id}`;
    const enCache = this._cacheSimple.get(clave);
    if (enCache) return enCache;
    const campo = this.dijkstra(this.grafo.fwd, nodo, metrica, franja.factores);
    if (this._cacheSimple.size >= 12) {
      this._cacheSimple.delete(this._cacheSimple.keys().next().value);
    }
    this._cacheSimple.set(clave, campo);
    return campo;
  }

  limpiarCache() {
    this._cache.clear();
    this._cacheSimple.clear();
  }
}

/**
 * Costo semanal para cada celda de la grilla de candidatos, en la unidad de la
 * métrica (minutos o kilómetros).
 *
 *   costo(celda) = Σ_i  viajes_i · (t_ida_i + t_vuelta_i) / 2
 *
 * `viajes` cuenta trayectos sueltos (ida y vuelta = 2), así que dividir por 2
 * convierte a viajes redondos y multiplicar por el costo de ida más vuelta da
 * el total semanal.
 */
export function campoDeCostoSemanal(grilla, camposPorPunto, pesos, metrica) {
  const costo = new Float32Array(grilla.n);
  const alcanzable = new Uint8Array(grilla.n);
  alcanzable.fill(1);

  for (let p = 0; p < camposPorPunto.length; p++) {
    const peso = pesos[p];
    if (!peso) continue;
    const { ida, vuelta, t0Ida, t0Vuelta } = camposPorPunto[p];
    const factor = peso / 2;
    for (let c = 0; c < grilla.n; c++) {
      if (!alcanzable[c]) continue;
      const nodo = grilla.nodo[c];
      const tIda = ida[nodo];
      const tVuelta = vuelta[nodo];
      if (!isFinite(tIda) || !isFinite(tVuelta)) {
        alcanzable[c] = 0;
        continue;
      }
      costo[c] += factor * (tIda + t0Ida + tVuelta + t0Vuelta);
    }
  }

  let mejor = -1;
  let mejorCosto = Infinity;
  let peorCosto = 0;
  for (let c = 0; c < grilla.n; c++) {
    if (!alcanzable[c]) {
      costo[c] = NaN;
      continue;
    }
    costo[c] /= metrica.divisor; // segundos -> minutos, o metros -> km
    if (costo[c] < mejorCosto) {
      mejorCosto = costo[c];
      mejor = c;
    }
    if (costo[c] > peorCosto) peorCosto = costo[c];
  }

  return { costo, mejor, mejorCosto, peorCosto };
}

/**
 * Detalle de un candidato: cuánto aporta cada punto al total semanal.
 */
export function desglosePorPunto(nodo, camposPorPunto, pesos, puntos, metrica) {
  const filas = [];
  let total = 0;
  for (let p = 0; p < camposPorPunto.length; p++) {
    const { ida, vuelta, t0Ida, t0Vuelta } = camposPorPunto[p];
    const costoIda = (ida[nodo] + t0Ida) / metrica.divisor;
    const costoVuelta = (vuelta[nodo] + t0Vuelta) / metrica.divisor;
    const semanal =
      isFinite(costoIda) && isFinite(costoVuelta) ? (pesos[p] / 2) * (costoIda + costoVuelta) : NaN;
    if (isFinite(semanal)) total += semanal;
    filas.push({
      punto: puntos[p],
      ida: costoIda,
      vuelta: costoVuelta,
      viajes: pesos[p],
      semanal,
    });
  }
  return { filas, total };
}

/**
 * Convierte un campo de costo por nodo en una imagen raster.
 *
 * Cada nodo "pinta" las celdas de raster a su alrededor con su propio tiempo,
 * quedándose siempre con el mínimo. Las celdas que ningún nodo alcanza quedan
 * en Infinity, y por eso la mancha resultante sigue la forma de las calles en
 * vez de ser un círculo: los tentáculos sobre las autopistas aparecen solos.
 */
export function rasterizarCampo(grafo, campo, bbox, celdaM, radioCeldas = 2) {
  const M_POR_GRADO_LAT = (Math.PI * 6371000) / 180;
  const latMedia = 0.5 * (bbox.lat_min + bbox.lat_max);
  const mPorGradoLon = M_POR_GRADO_LAT * Math.cos((latMedia * Math.PI) / 180);

  const pasoLat = celdaM / M_POR_GRADO_LAT;
  const pasoLon = celdaM / mPorGradoLon;
  // El +1 es para que el índice de celda de un nodo pegado al borde superior
  // del bbox siga cayendo dentro del raster.
  const ancho = Math.ceil((bbox.lon_max - bbox.lon_min) / pasoLon) + 1;
  const alto = Math.ceil((bbox.lat_max - bbox.lat_min) / pasoLat) + 1;

  const raster = new Float32Array(ancho * alto);
  raster.fill(Infinity);

  for (let i = 0; i < grafo.n; i++) {
    const valor = campo[i];
    if (!isFinite(valor)) continue;
    const cx = Math.round((grafo.lon[i] - bbox.lon_min) / pasoLon);
    const cy = Math.round((grafo.lat[i] - bbox.lat_min) / pasoLat);
    const x0 = Math.max(0, cx - radioCeldas);
    const x1 = Math.min(ancho - 1, cx + radioCeldas);
    const y0 = Math.max(0, cy - radioCeldas);
    const y1 = Math.min(alto - 1, cy + radioCeldas);
    for (let y = y0; y <= y1; y++) {
      const fila = y * ancho;
      for (let x = x0; x <= x1; x++) {
        if (valor < raster[fila + x]) raster[fila + x] = valor;
      }
    }
  }

  return { raster, ancho, alto, bbox, pasoLat, pasoLon };
}
