"""
Utilidades geométricas: distancias, ensamblado de polígonos comunales desde las
relaciones de OSM, punto-en-polígono y búsqueda del nodo más cercano.

Todo lo que se aplica a muchos puntos a la vez está vectorizado con numpy. En
Python puro, evaluar 36.000 celdas de grilla contra siete polígonos comunales de
un par de miles de vértices cada uno se va a la media hora; vectorizado sobre el
eje de los puntos baja a segundos.
"""

import math

import numpy as np

# Radio terrestre medio. A la latitud de Santiago el error de usar una
# aproximación equirectangular en distancias de pocos km es < 0.1%, muy por
# debajo de la incertidumbre del modelo de tráfico.
R_TIERRA_M = 6_371_000.0
M_POR_GRADO_LAT = math.pi * R_TIERRA_M / 180.0


def metros_por_grado(lat_deg: float) -> tuple[float, float]:
    """Metros por grado de latitud y de longitud a una latitud dada."""
    return M_POR_GRADO_LAT, M_POR_GRADO_LAT * math.cos(math.radians(lat_deg))


def distancia_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia equirectangular en metros."""
    _, m_lon = metros_por_grado(0.5 * (lat1 + lat2))
    dx = (lon2 - lon1) * m_lon
    dy = (lat2 - lat1) * M_POR_GRADO_LAT
    return math.hypot(dx, dy)


# ---------------------------------------------------------------------------
# Polígonos comunales
# ---------------------------------------------------------------------------


def ensamblar_anillos(relacion: dict) -> list[np.ndarray]:
    """
    Convierte una relación `boundary=administrative` de OSM (obtenida con
    `out geom`) en una lista de anillos cerrados, cada uno un array (n, 2) de
    (lat, lon).

    OSM entrega el borde como un montón de vías sueltas, en orden arbitrario y
    con orientación arbitraria. Hay que coserlas: se toma un tramo, se busca
    otro que empiece o termine en su punta, y así hasta cerrar el anillo.
    """
    tramos: list[list[tuple[float, float]]] = []
    for miembro in relacion.get("members", []):
        if miembro.get("type") != "way":
            continue
        if miembro.get("role") not in ("outer", "", None):
            continue
        geometria = miembro.get("geometry")
        if not geometria:
            continue
        puntos = [(p["lat"], p["lon"]) for p in geometria if p.get("lat") is not None]
        if len(puntos) >= 2:
            tramos.append(puntos)

    anillos: list[np.ndarray] = []
    pendientes = tramos[:]

    while pendientes:
        actual = pendientes.pop()
        avanzo = True
        while avanzo and actual[0] != actual[-1]:
            avanzo = False
            for indice, tramo in enumerate(pendientes):
                if tramo[0] == actual[-1]:
                    actual.extend(tramo[1:])
                elif tramo[-1] == actual[-1]:
                    actual.extend(reversed(tramo[:-1]))
                elif tramo[-1] == actual[0]:
                    actual = tramo[:-1] + actual
                elif tramo[0] == actual[0]:
                    actual = list(reversed(tramo[1:])) + actual
                else:
                    continue
                pendientes.pop(indice)
                avanzo = True
                break
        if len(actual) >= 4:
            if actual[0] != actual[-1]:
                actual.append(actual[0])
            anillos.append(np.asarray(actual, dtype=np.float64))

    return anillos


def _dentro_de_anillo(lats: np.ndarray, lons: np.ndarray, anillo: np.ndarray) -> np.ndarray:
    """
    Ray casting vectorizado sobre los PUNTOS. Se itera por arista del anillo
    (unos miles) y cada iteración procesa los 36.000 puntos de una vez.
    """
    dentro = np.zeros(len(lats), dtype=bool)
    lat_a, lon_a = anillo[:-1, 0], anillo[:-1, 1]
    lat_b, lon_b = anillo[1:, 0], anillo[1:, 1]

    for k in range(len(lat_a)):
        cruza = (lon_a[k] > lons) != (lon_b[k] > lons)
        indices = np.nonzero(cruza)[0]
        if indices.size == 0:
            continue
        # `cruza` garantiza lon_a != lon_b, así que la división es segura.
        pendiente = (lat_b[k] - lat_a[k]) / (lon_b[k] - lon_a[k])
        lat_corte = lat_a[k] + (lons[indices] - lon_a[k]) * pendiente
        dentro[indices] ^= lats[indices] < lat_corte

    return dentro


class MascaraArea:
    """Unión de polígonos comunales y rectángulos manuales."""

    def __init__(self) -> None:
        self._anillos: list[np.ndarray] = []
        self._cajas: list[dict] = []

    def agregar_relacion(self, relacion: dict) -> int:
        anillos = ensamblar_anillos(relacion)
        self._anillos.extend(anillos)
        return len(anillos)

    def agregar_caja(self, caja: dict) -> None:
        self._cajas.append(caja)

    def dentro(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        """Máscara booleana: qué puntos caen dentro del área."""
        resultado = np.zeros(len(lats), dtype=bool)

        for caja in self._cajas:
            resultado |= (
                (lats >= caja["lat_min"])
                & (lats <= caja["lat_max"])
                & (lons >= caja["lon_min"])
                & (lons <= caja["lon_max"])
            )

        for anillo in self._anillos:
            # Descarte rápido por bounding box antes del ray casting.
            lat_min, lat_max = anillo[:, 0].min(), anillo[:, 0].max()
            lon_min, lon_max = anillo[:, 1].min(), anillo[:, 1].max()
            cerca = np.nonzero(
                (lats >= lat_min) & (lats <= lat_max) & (lons >= lon_min) & (lons <= lon_max)
            )[0]
            if cerca.size == 0:
                continue
            resultado[cerca] |= _dentro_de_anillo(lats[cerca], lons[cerca], anillo)

        return resultado

    def bbox(self) -> dict:
        lats: list[float] = []
        lons: list[float] = []
        for anillo in self._anillos:
            lats += [float(anillo[:, 0].min()), float(anillo[:, 0].max())]
            lons += [float(anillo[:, 1].min()), float(anillo[:, 1].max())]
        for caja in self._cajas:
            lats += [caja["lat_min"], caja["lat_max"]]
            lons += [caja["lon_min"], caja["lon_max"]]
        if not lats:
            raise ValueError("La máscara está vacía: no se cargó ninguna comuna ni rectángulo.")
        return {
            "lat_min": min(lats),
            "lat_max": max(lats),
            "lon_min": min(lons),
            "lon_max": max(lons),
        }


# ---------------------------------------------------------------------------
# Nodo más cercano
# ---------------------------------------------------------------------------


class IndiceNodos:
    """
    Grilla de buckets sobre un subconjunto de nodos del grafo.

    Se construye solo con los nodos candidatos (por ejemplo los habitables), y
    devuelve índices dentro de ese subconjunto: filtrar antes es mucho más
    barato que filtrar en cada consulta.
    """

    def __init__(self, lats: np.ndarray, lons: np.ndarray, celda_m: float = 400.0):
        self.lats = np.asarray(lats, dtype=np.float64)
        self.lons = np.asarray(lons, dtype=np.float64)
        self.celda_m = celda_m

        lat_ref = float(self.lats.mean()) if self.lats.size else -33.4
        # Un solo factor de longitud para todo el índice: entre los extremos del
        # área varía 0.4%, que sobre un radio de 300 m es poco más de un metro.
        self.m_lon = M_POR_GRADO_LAT * math.cos(math.radians(lat_ref))
        self.paso_lat = celda_m / M_POR_GRADO_LAT
        self.paso_lon = celda_m / self.m_lon

        filas = np.floor(self.lats / self.paso_lat).astype(np.int64)
        columnas = np.floor(self.lons / self.paso_lon).astype(np.int64)
        self._celdas: dict[tuple[int, int], np.ndarray] = {}
        orden = np.lexsort((columnas, filas))
        claves = list(zip(filas[orden].tolist(), columnas[orden].tolist()))
        inicio = 0
        for pos in range(1, len(claves) + 1):
            if pos == len(claves) or claves[pos] != claves[inicio]:
                self._celdas[claves[inicio]] = orden[inicio:pos]
                inicio = pos

    def _clave(self, lat: float, lon: float) -> tuple[int, int]:
        return (
            int(math.floor(lat / self.paso_lat)),
            int(math.floor(lon / self.paso_lon)),
        )

    def mas_cercano_masivo(
        self, lats: np.ndarray, lons: np.ndarray, radio_max_m: float
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Para cada punto consultado, el nodo más cercano dentro del radio.
        Devuelve (indices, distancias); indice -1 si no hay ninguno.

        El radio máximo tiene que ser menor que el tamaño de celda: así basta
        con revisar el bloque de 3x3 celdas alrededor de la consulta. Los
        candidatos de cada bloque se memoizan, porque muchas consultas caen en
        la misma celda.
        """
        if radio_max_m >= self.celda_m:
            raise ValueError(
                f"radio_max_m ({radio_max_m}) debe ser menor que celda_m ({self.celda_m})"
            )

        n = len(lats)
        mejores = np.full(n, -1, dtype=np.int64)
        distancias = np.full(n, np.inf)
        radio2 = radio_max_m * radio_max_m
        cache: dict[tuple[int, int], np.ndarray] = {}

        for q in range(n):
            clave = self._clave(lats[q], lons[q])
            candidatos = cache.get(clave)
            if candidatos is None:
                trozos = [
                    self._celdas[(clave[0] + di, clave[1] + dj)]
                    for di in (-1, 0, 1)
                    for dj in (-1, 0, 1)
                    if (clave[0] + di, clave[1] + dj) in self._celdas
                ]
                candidatos = (
                    np.concatenate(trozos) if trozos else np.empty(0, dtype=np.int64)
                )
                cache[clave] = candidatos
            if candidatos.size == 0:
                continue

            dy = (self.lats[candidatos] - lats[q]) * M_POR_GRADO_LAT
            dx = (self.lons[candidatos] - lons[q]) * self.m_lon
            d2 = dy * dy + dx * dx
            k = int(np.argmin(d2))
            if d2[k] <= radio2:
                mejores[q] = candidatos[k]
                distancias[q] = math.sqrt(float(d2[k]))

        return mejores, distancias
