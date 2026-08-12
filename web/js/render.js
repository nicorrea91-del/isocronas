/**
 * Pintado de capas: isócronas en bandas y mapa de calor del costo semanal.
 *
 * Todo se dibuja en un canvas fuera de pantalla y se monta en el mapa como una
 * sola imagen georreferenciada. Es muchísimo más liviano que meter 20.000
 * polígonos: una imagen de 300x300 se pinta en un par de milisegundos y la GPU
 * la escala sola al hacer zoom.
 */

// Rampa del mapa de calor: verde (pocos minutos a la semana) a rojo oscuro.
export const RAMPA_COSTO = [
  [0, 104, 55],
  [49, 163, 84],
  [166, 217, 106],
  [255, 255, 191],
  [253, 174, 97],
  [244, 109, 67],
  [165, 0, 38],
];

// Colores de las bandas de isócrona, de la más cercana a la más lejana.
export const COLORES_BANDA = [
  [26, 152, 80],
  [253, 174, 97],
  [215, 48, 39],
];

function interpolar(rampa, t) {
  const x = Math.min(1, Math.max(0, t)) * (rampa.length - 1);
  const i = Math.min(rampa.length - 2, Math.floor(x));
  const f = x - i;
  const a = rampa[i];
  const b = rampa[i + 1];
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ];
}

/**
 * Esquinas geográficas de una imagen cuyos píxeles son celdas CENTRADAS en
 * lon_min + ix·paso. Hay que expandir media celda a cada lado, si no la capa
 * queda corrida medio píxel respecto al mapa.
 */
function esquinas(bbox, pasoLat, pasoLon, ancho, alto) {
  const oeste = bbox.lon_min - pasoLon / 2;
  const este = bbox.lon_min + (ancho - 1) * pasoLon + pasoLon / 2;
  const sur = bbox.lat_min - pasoLat / 2;
  const norte = bbox.lat_min + (alto - 1) * pasoLat + pasoLat / 2;
  return [
    [oeste, norte],
    [este, norte],
    [este, sur],
    [oeste, sur],
  ];
}

function nuevoCanvas(ancho, alto) {
  const canvas = document.createElement('canvas');
  canvas.width = ancho;
  canvas.height = alto;
  return canvas;
}

/**
 * Isócronas: cada umbral de minutos pinta una banda. Las celdas que ningún
 * nodo alcanza quedan transparentes, y ahí está el truco visual: el borde
 * sigue las calles, con los tentáculos sobre las autopistas.
 */
export function pintarIsocronas(rasterizado, umbralesUsuario, factorUnidad, alfa = 0.38) {
  const { raster, ancho, alto, bbox, pasoLat, pasoLon } = rasterizado;
  // El raster está en unidades crudas (segundos o metros) y los umbrales que
  // mueve el usuario están en minutos o kilómetros.
  const umbrales = umbralesUsuario
    .filter((valor) => valor > 0)
    .sort((a, b) => a - b)
    .map((valor) => valor * factorUnidad);
  if (umbrales.length === 0) return null;

  const canvas = nuevoCanvas(ancho, alto);
  const contexto = canvas.getContext('2d');
  const imagen = contexto.createImageData(ancho, alto);
  const datos = imagen.data;

  // Banda de cada celda: -1 = fuera de todas las isócronas.
  const banda = new Int8Array(ancho * alto);
  banda.fill(-1);
  for (let i = 0; i < raster.length; i++) {
    const valor = raster[i];
    if (!isFinite(valor)) continue;
    for (let b = 0; b < umbrales.length; b++) {
      if (valor <= umbrales[b]) {
        banda[i] = b;
        break;
      }
    }
  }

  const alfaRelleno = Math.round(alfa * 255);
  for (let y = 0; y < alto; y++) {
    // El raster crece hacia el norte; el canvas hacia el sur.
    const filaRaster = (alto - 1 - y) * ancho;
    const filaCanvas = y * ancho;
    for (let x = 0; x < ancho; x++) {
      const b = banda[filaRaster + x];
      const destino = (filaCanvas + x) * 4;
      if (b < 0) {
        datos[destino + 3] = 0;
        continue;
      }
      const color = COLORES_BANDA[Math.min(b, COLORES_BANDA.length - 1)];

      // Borde: si algún vecino pertenece a otra banda, se oscurece el píxel.
      // Da la línea de contorno sin tener que calcular polígonos.
      let esBorde = false;
      const px = filaRaster + x;
      if (x === 0 || x === ancho - 1 || y === 0 || y === alto - 1) {
        esBorde = false;
      } else {
        esBorde =
          banda[px - 1] !== b ||
          banda[px + 1] !== b ||
          banda[px - ancho] !== b ||
          banda[px + ancho] !== b;
      }

      if (esBorde) {
        datos[destino] = Math.round(color[0] * 0.55);
        datos[destino + 1] = Math.round(color[1] * 0.55);
        datos[destino + 2] = Math.round(color[2] * 0.55);
        datos[destino + 3] = 235;
      } else {
        datos[destino] = color[0];
        datos[destino + 1] = color[1];
        datos[destino + 2] = color[2];
        datos[destino + 3] = alfaRelleno;
      }
    }
  }

  contexto.putImageData(imagen, 0, 0);
  return {
    url: canvas.toDataURL('image/png'),
    coordenadas: esquinas(bbox, pasoLat, pasoLon, ancho, alto),
  };
}

/**
 * Mapa de calor del costo semanal sobre la grilla de candidatos.
 *
 * La escala se corta en el percentil 95 para que un par de celdas malísimas
 * (una parcela al final de un camino de tierra) no aplasten todo el resto del
 * gradiente hacia el verde.
 */
export function pintarHeatmapCosto(grilla, costo, alfa = 0.55) {
  const finitos = [];
  for (let c = 0; c < grilla.n; c++) {
    if (isFinite(costo[c])) finitos.push(costo[c]);
  }
  if (finitos.length === 0) return null;

  finitos.sort((a, b) => a - b);
  const minimo = finitos[0];
  const maximo = finitos[Math.floor(finitos.length * 0.95)] || finitos[finitos.length - 1];
  const rango = Math.max(1e-6, maximo - minimo);

  const canvas = nuevoCanvas(grilla.nx, grilla.ny);
  const contexto = canvas.getContext('2d');
  const imagen = contexto.createImageData(grilla.nx, grilla.ny);
  const datos = imagen.data;
  const alfaRelleno = Math.round(alfa * 255);

  for (let c = 0; c < grilla.n; c++) {
    const valor = costo[c];
    if (!isFinite(valor)) continue;
    const x = grilla.ix[c];
    const y = grilla.ny - 1 - grilla.iy[c];
    const color = interpolar(RAMPA_COSTO, (valor - minimo) / rango);
    const destino = (y * grilla.nx + x) * 4;
    datos[destino] = color[0];
    datos[destino + 1] = color[1];
    datos[destino + 2] = color[2];
    datos[destino + 3] = alfaRelleno;
  }

  contexto.putImageData(imagen, 0, 0);
  return {
    url: canvas.toDataURL('image/png'),
    coordenadas: esquinas(grilla.bbox, grilla.pasoLat, grilla.pasoLon, grilla.nx, grilla.ny),
    minimo,
    maximo,
  };
}

export function colorDeCosto(valor, minimo, maximo) {
  const t = (valor - minimo) / Math.max(1e-6, maximo - minimo);
  const [r, g, b] = interpolar(RAMPA_COSTO, t);
  return `rgb(${r},${g},${b})`;
}

export function formatearMinutos(minutos) {
  if (!isFinite(minutos)) return '—';
  if (minutos < 60) return `${minutos.toFixed(0)} min`;
  const horas = Math.floor(minutos / 60);
  const resto = Math.round(minutos - horas * 60);
  return resto === 0 ? `${horas} h` : `${horas} h ${resto} min`;
}

export function formatearCosto(valor, metrica) {
  if (!isFinite(valor)) return '—';
  if (metrica.unidad === 'km') {
    return valor < 10 ? `${valor.toFixed(1)} km` : `${Math.round(valor)} km`;
  }
  return formatearMinutos(valor);
}
