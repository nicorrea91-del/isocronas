"""
Descarga desde OpenStreetMap todo lo que el proyecto necesita:

  1. La red viaria transitable en auto del rectángulo de ruteo.
  2. Los límites comunales (admin_level 8) para la zona de análisis.

Se usa la API Overpass, que es gratuita. La descarga se hace por tiles y se
cachea en disco comprimida, así que solo se paga el costo una vez. Overpass
devuelve las vías completas cuando intersectan el tile, de modo que los tiles
se solapan solos y no hay que preocuparse de calles cortadas en el borde.

Dos detalles que hacen la diferencia contra las instancias públicas:

* **Consultas por tipo exacto en vez de regex.** `way[highway=motorway]` usa el
  índice de tags; `way[highway~"^(motorway|trunk|...)$"]` obliga a Overpass a
  evaluar la expresión contra un montón de objetos y es la causa principal de
  los timeouts.

* **Subdivisión automática.** Si un tile da 504 (el servidor se rindió), se
  parte en cuatro y se reintenta cada cuadrante. Los tiles del centro de
  Santiago son mucho más densos que los de Colina, así que una grilla fija
  siempre queda mal calibrada para alguna parte del mapa.

Uso:
    python pipeline/fetch_osm.py            # descarga lo que falte
    python pipeline/fetch_osm.py --recargar # ignora la caché
"""

import gzip
import json
import sys
import time
import urllib.error
import urllib.request

from config import (
    CACHE_DIR,
    GRAPH_BBOX,
    INCLUDED_HIGHWAYS,
)

# Mirrors públicos de Overpass. Si uno falla o está saturado, se prueba el
# siguiente antes de reintentar.
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

USER_AGENT = "proyecto-isocronas/0.1 (uso personal, analisis de centro de gravedad)"

# Grilla inicial. Los tiles que el servidor no aguante se subdividen solos.
TILES_LAT = 4
TILES_LON = 4
MAX_PROFUNDIDAD = 3

PAUSA_ENTRE_REQUESTS_S = 2.0
# Intentos por tile antes de decidir que el tile es muy grande y subdividirlo.
MAX_INTENTOS = 3
# Los 429 son límite de tasa, no de tamaño: subdividir no ayuda, esperar sí.
MAX_ESPERAS_POR_CUOTA = 6
ESPERA_POR_CUOTA_S = 30


class TileMuyGrande(Exception):
    """El servidor no alcanzó a responder: hay que partir el tile."""


def _post(endpoint: str, query: str, timeout: int = 300) -> bytes:
    request = urllib.request.Request(
        endpoint,
        data=query.encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "text/plain; charset=utf-8",
            "Accept-Encoding": "gzip",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


def overpass(query: str, cache_key: str, recargar: bool = False) -> dict:
    """
    Ejecuta una consulta Overpass con caché en disco.

    Lanza TileMuyGrande si el servidor devuelve timeouts de forma persistente.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{cache_key}.json.gz"

    if cache_file.exists() and not recargar:
        with gzip.open(cache_file, "rt", encoding="utf-8") as fh:
            return json.load(fh)

    intentos = 0
    esperas_cuota = 0
    ultimo_error: Exception | None = None

    while intentos < MAX_INTENTOS:
        endpoint = ENDPOINTS[(intentos + esperas_cuota) % len(ENDPOINTS)]
        try:
            print(f"    -> {endpoint.split('/')[2]}", flush=True)
            payload = json.loads(_post(endpoint, query))
            if "elements" not in payload:
                raise ValueError("respuesta sin 'elements'")
            with gzip.open(cache_file, "wt", encoding="utf-8") as fh:
                json.dump(payload, fh)
            return payload

        except urllib.error.HTTPError as exc:
            ultimo_error = exc
            if exc.code == 429 and esperas_cuota < MAX_ESPERAS_POR_CUOTA:
                esperas_cuota += 1
                print(
                    f"       429 (límite de tasa), espera {ESPERA_POR_CUOTA_S}s "
                    f"[{esperas_cuota}/{MAX_ESPERAS_POR_CUOTA}]",
                    flush=True,
                )
                time.sleep(ESPERA_POR_CUOTA_S)
                continue
            intentos += 1
            print(f"       HTTP {exc.code}, intento {intentos}/{MAX_INTENTOS}", flush=True)
            time.sleep(5 * intentos)

        except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
            ultimo_error = exc
            intentos += 1
            print(f"       {type(exc).__name__}, intento {intentos}/{MAX_INTENTOS}", flush=True)
            time.sleep(5 * intentos)

    raise TileMuyGrande(f"{cache_key}: {ultimo_error}")


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------


def _caja(bbox: dict) -> str:
    return (
        f'{bbox["lat_min"]:.6f},{bbox["lon_min"]:.6f},'
        f'{bbox["lat_max"]:.6f},{bbox["lon_max"]:.6f}'
    )


def _query_vias(bbox: dict) -> str:
    caja = _caja(bbox)
    # Una sentencia por tipo de vía: cada una usa el índice de tags.
    lineas = "\n".join(f'  way["highway"="{clase}"]({caja});' for clase in INCLUDED_HIGHWAYS)
    # Las vías van con tags (necesitamos highway, oneway, maxspeed, access y la
    # lista de nodos para la topología). Los nodos van en formato `skel`, que
    # trae solo id y coordenadas: pedirlos con tags triplicaría el peso.
    return f"""[out:json][timeout:280];
(
{lineas}
)->.vias;
.vias out body;
node(w.vias);
out skel qt;
"""


def _query_comunas() -> str:
    return f"""[out:json][timeout:280];
relation["boundary"="administrative"]["admin_level"="8"]({_caja(GRAPH_BBOX)});
out geom;
"""


# ---------------------------------------------------------------------------
# Descarga con subdivisión
# ---------------------------------------------------------------------------


def _subdividir(bbox: dict) -> list[dict]:
    lat_medio = 0.5 * (bbox["lat_min"] + bbox["lat_max"])
    lon_medio = 0.5 * (bbox["lon_min"] + bbox["lon_max"])
    return [
        {"lat_min": bbox["lat_min"], "lat_max": lat_medio, "lon_min": bbox["lon_min"], "lon_max": lon_medio},
        {"lat_min": bbox["lat_min"], "lat_max": lat_medio, "lon_min": lon_medio, "lon_max": bbox["lon_max"]},
        {"lat_min": lat_medio, "lat_max": bbox["lat_max"], "lon_min": bbox["lon_min"], "lon_max": lon_medio},
        {"lat_min": lat_medio, "lat_max": bbox["lat_max"], "lon_min": lon_medio, "lon_max": bbox["lon_max"]},
    ]


def _descargar_tile(bbox: dict, cache_key: str, recargar: bool, acumulador: dict, profundidad: int = 0) -> None:
    try:
        payload = overpass(_query_vias(bbox), cache_key, recargar)
    except TileMuyGrande as exc:
        if profundidad >= MAX_PROFUNDIDAD:
            raise RuntimeError(
                f"El tile {cache_key} falla incluso subdividido al máximo: {exc}"
            ) from exc
        print(f"    {cache_key} muy grande, se parte en 4.", flush=True)
        for indice, sub in enumerate(_subdividir(bbox)):
            _descargar_tile(sub, f"{cache_key}_{indice}", recargar, acumulador, profundidad + 1)
        return

    for element in payload["elements"]:
        if element["type"] == "way":
            acumulador["ways"][element["id"]] = element
        elif element["type"] == "node":
            acumulador["nodes"][element["id"]] = (element["lat"], element["lon"])


def _tiles_iniciales() -> list[tuple[str, dict]]:
    lat_step = (GRAPH_BBOX["lat_max"] - GRAPH_BBOX["lat_min"]) / TILES_LAT
    lon_step = (GRAPH_BBOX["lon_max"] - GRAPH_BBOX["lon_min"]) / TILES_LON
    tiles = []
    for i in range(TILES_LAT):
        for j in range(TILES_LON):
            tiles.append(
                (
                    f"vias_{i}_{j}",
                    {
                        "lat_min": GRAPH_BBOX["lat_min"] + i * lat_step,
                        "lat_max": GRAPH_BBOX["lat_min"] + (i + 1) * lat_step,
                        "lon_min": GRAPH_BBOX["lon_min"] + j * lon_step,
                        "lon_max": GRAPH_BBOX["lon_min"] + (j + 1) * lon_step,
                    },
                )
            )
    return tiles


def fetch_vias(recargar: bool = False) -> dict:
    """Descarga la red viaria por tiles y la fusiona deduplicando por id."""
    tiles = _tiles_iniciales()
    acumulador: dict = {"ways": {}, "nodes": {}}

    for indice, (cache_key, bbox) in enumerate(tiles, start=1):
        ya_estaba = (CACHE_DIR / f"{cache_key}.json.gz").exists() and not recargar
        print(f"Tile {indice}/{len(tiles)}: {cache_key}", flush=True)
        _descargar_tile(bbox, cache_key, recargar, acumulador)
        if not ya_estaba and indice < len(tiles):
            time.sleep(PAUSA_ENTRE_REQUESTS_S)

    print(f"\nRed viaria: {len(acumulador['ways']):,} vías y {len(acumulador['nodes']):,} nodos únicos.")
    return acumulador


def fetch_comunas(recargar: bool = False) -> list[dict]:
    """Descarga los límites comunales (admin_level 8) del rectángulo."""
    print("Límites comunales", flush=True)
    payload = overpass(_query_comunas(), "comunas", recargar)
    relaciones = [e for e in payload["elements"] if e["type"] == "relation"]
    nombres = sorted({r.get("tags", {}).get("name", "?") for r in relaciones})
    print(f"  {len(relaciones)} comunas: {', '.join(nombres)}")
    return relaciones


def main() -> int:
    recargar = "--recargar" in sys.argv
    inicio = time.time()

    fetch_comunas(recargar)
    print()
    datos = fetch_vias(recargar)

    faltantes = sum(
        1
        for way in datos["ways"].values()
        for node_id in way.get("nodes", [])
        if node_id not in datos["nodes"]
    )
    if faltantes:
        # No debería pasar: Overpass devuelve las vías completas. Si ocurre,
        # build_graph descarta esos tramos y lo reporta.
        print(f"Aviso: {faltantes:,} referencias a nodos sin coordenadas.")

    print(f"\nListo en {time.time() - inicio:.0f}s. Caché en {CACHE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
