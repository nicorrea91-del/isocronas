"""
Construye el grafo de ruteo a partir de la caché de OpenStreetMap y lo exporta
en un binario compacto que el navegador carga de una sola vez.

Decisiones de diseño que importan:

* **Contracción de nodos grado 2.** OSM guarda cada curva como decenas de
  nodos. Para rutear solo interesan las intersecciones, así que los nodos
  intermedios se colapsan en una sola arista que conserva el largo real de la
  polilínea. Esto baja el grafo a menos de la mitad.

* **Densificación a 350 m.** Contraer a secas dejaría aristas de autopista de
  4 km sin ningún punto intermedio, y como las isócronas se dibujan muestreando
  el campo de costo en los nodos, la mancha quedaría corta justo en las
  autopistas, que es donde más importa. Así que toda arista más larga que 350 m
  se vuelve a partir en los puntos de forma originales.

* **Solo la componente conexa mayor.** Los tiles de Overpass dejan islas
  sueltas (estacionamientos, callejones aislados). Se descartan.

Uso:
    python pipeline/build_graph.py
"""

import json
import sys
from datetime import datetime

import numpy as np

import fetch_osm
from config import (
    ANALYSIS_COMUNAS,
    ANALYSIS_EXTRA_BOXES,
    CLASS_GROUPS,
    FREEFLOW_KMH,
    GRAPH_BBOX,
    GRID_SPACING_M,
    GROUP_OF_HIGHWAY,
    WEB_DATA_DIR,
)
from geometry import IndiceNodos, MascaraArea, distancia_m, metros_por_grado

# Largo máximo de arista antes de volver a partirla (ver docstring).
MAX_ARISTA_M = 350.0

# Radio máximo para enganchar una celda de la grilla a una calle. Más allá de
# esto la celda es cerro, río, cancha de golf o Parque Metropolitano: no se
# puede vivir ahí y el tiempo de viaje sería una fantasía.
RADIO_SNAP_M = 300.0

GRUPOS = list(CLASS_GROUPS.keys())
INDICE_GRUPO = {nombre: i for i, nombre in enumerate(GRUPOS)}

# Vías donde la gente no vive: una celda de la grilla que solo tenga acceso a
# una autopista no es un lugar habitable, es la berma.
GRUPO_NO_HABITABLE = INDICE_GRUPO["autopista"]


# ---------------------------------------------------------------------------
# Lectura de tags
# ---------------------------------------------------------------------------


def _es_privada(tags: dict) -> bool:
    """
    Calles a las que no se puede entrar en auto: condominios cerrados, accesos
    de servicio, caminos con barrera. Se filtra acá y no en la consulta Overpass
    porque un filtro de regex sobre `access` hace mucho más lenta la descarga.
    """
    for llave in ("access", "motor_vehicle", "vehicle"):
        valor = str(tags.get(llave, "")).strip().lower()
        if valor in ("private", "no", "customers", "permit"):
            # `motor_vehicle=yes` explícito manda sobre un `access=private`
            # genérico (típico de calles interiores señalizadas).
            if llave == "access" and str(tags.get("motor_vehicle", "")).lower() in ("yes", "designated", "permissive"):
                continue
            return True
    return False


def parse_oneway(tags: dict, highway: str) -> int:
    """1 = solo A->B, -1 = solo B->A, 0 = doble sentido."""
    valor = str(tags.get("oneway", "")).strip().lower()
    if valor in ("yes", "true", "1"):
        return 1
    if valor in ("-1", "reverse"):
        return -1
    if valor in ("no", "false", "0"):
        return 0
    # Sentido único implícito por convención de OSM.
    if str(tags.get("junction", "")).lower() in ("roundabout", "circular"):
        return 1
    if highway in ("motorway", "motorway_link"):
        return 1
    return 0


def parse_maxspeed(tags: dict) -> float | None:
    raw = tags.get("maxspeed")
    if not raw:
        return None
    texto = str(raw).strip().lower()
    if texto in ("none", "signals", "variable", "walk"):
        return None
    try:
        valor = float(texto.split()[0].replace(",", "."))
    except (ValueError, IndexError):
        return None
    if 5.0 <= valor <= 130.0:
        return valor
    return None


def velocidad_flujo_libre(tags: dict, highway: str) -> float:
    """
    Velocidad en km/h sin tráfico. Se parte del tipo de vía y se corrige con
    `maxspeed` cuando está etiquetado, tomando el menor de los dos: el límite
    legal es un techo, y en calles con muchos semáforos el tipo de vía ya
    refleja mejor la velocidad real de recorrido.
    """
    base = FREEFLOW_KMH.get(highway, 30)
    limite = parse_maxspeed(tags)
    if limite is None:
        return float(base)
    return float(min(base, limite)) if limite < base else float(base)


# ---------------------------------------------------------------------------
# Construcción del grafo
# ---------------------------------------------------------------------------


def construir_aristas(ways: dict, nodos_osm: dict):
    """
    Devuelve (aristas, ids_usados) donde cada arista es
    (osm_a, osm_b, largo_m, velocidad_kmh, grupo, doble_sentido).
    """
    # Cuántas vías distintas usan cada nodo. Un nodo compartido por dos vías es
    # una intersección y no se puede contraer.
    usos: dict[int, int] = {}
    vias_validas = []
    descartadas_privadas = 0

    for way in ways.values():
        tags = way.get("tags", {})
        highway = tags.get("highway")
        if highway not in FREEFLOW_KMH:
            continue
        if _es_privada(tags):
            descartadas_privadas += 1
            continue
        refs = [ref for ref in way.get("nodes", []) if ref in nodos_osm]
        if len(refs) < 2:
            continue
        vias_validas.append((way, tags, highway, refs))
        for ref in refs:
            usos[ref] = usos.get(ref, 0) + 1

    if descartadas_privadas:
        print(f"  {descartadas_privadas:,} vías privadas o cerradas descartadas.")

    aristas = []
    descartadas_largo_cero = 0

    for _way, tags, highway, refs in vias_validas:
        sentido = parse_oneway(tags, highway)
        velocidad = velocidad_flujo_libre(tags, highway)
        grupo = INDICE_GRUPO[GROUP_OF_HIGHWAY[highway]]
        doble = sentido == 0

        # Un solo recorrido: se corta en intersecciones, en los extremos, y
        # cada vez que el tramo acumulado supera MAX_ARISTA_M.
        inicio_tramo = 0
        acumulado = 0.0
        for pos in range(1, len(refs)):
            lat_a, lon_a = nodos_osm[refs[pos - 1]]
            lat_b, lon_b = nodos_osm[refs[pos]]
            acumulado += distancia_m(lat_a, lon_a, lat_b, lon_b)

            es_interseccion = usos.get(refs[pos], 0) > 1
            es_final = pos == len(refs) - 1
            if es_interseccion or es_final or acumulado >= MAX_ARISTA_M:
                nodo_a, nodo_b = refs[inicio_tramo], refs[pos]
                if nodo_a != nodo_b and acumulado > 0.5:
                    if sentido >= 0:
                        aristas.append((nodo_a, nodo_b, acumulado, velocidad, grupo, doble))
                    else:
                        aristas.append((nodo_b, nodo_a, acumulado, velocidad, grupo, doble))
                else:
                    descartadas_largo_cero += 1
                inicio_tramo = pos
                acumulado = 0.0

    if descartadas_largo_cero:
        print(f"  {descartadas_largo_cero:,} tramos degenerados descartados.")
    return aristas


def componente_mayor(aristas, nodos_presentes):
    """Union-find sobre la vista no dirigida; devuelve el set de nodos de la componente mayor."""
    padre = {nodo: nodo for nodo in nodos_presentes}

    def raiz(x):
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x

    for nodo_a, nodo_b, *_ in aristas:
        ra, rb = raiz(nodo_a), raiz(nodo_b)
        if ra != rb:
            padre[ra] = rb

    componentes: dict[int, list[int]] = {}
    for nodo in nodos_presentes:
        componentes.setdefault(raiz(nodo), []).append(nodo)

    mayor = max(componentes.values(), key=len)
    print(
        f"  {len(componentes):,} componentes; la mayor tiene "
        f"{len(mayor):,} nodos ({100 * len(mayor) / len(nodos_presentes):.1f}%)."
    )
    return set(mayor)


def construir_csr(aristas, nodos_osm):
    """
    Reindexa los nodos a 0..N-1 y arma la lista de adyacencia en formato CSR.
    Devuelve las arrays listas para exportar más un mapa osm_id -> indice.
    """
    ids = sorted({nodo for arista in aristas for nodo in arista[:2]})
    indice_de = {osm_id: i for i, osm_id in enumerate(ids)}
    n = len(ids)

    lats = np.array([nodos_osm[osm_id][0] for osm_id in ids], dtype=np.float64)
    lons = np.array([nodos_osm[osm_id][1] for osm_id in ids], dtype=np.float64)

    # Cada arista de doble sentido genera dos entradas dirigidas.
    dirigidas = []
    habitable = np.zeros(n, dtype=bool)
    for nodo_a, nodo_b, largo, velocidad, grupo, doble in aristas:
        ia, ib = indice_de[nodo_a], indice_de[nodo_b]
        # Tiempo de flujo libre en décimas de segundo.
        decisegundos = int(round(largo / (velocidad * 1000.0 / 3600.0) * 10.0))
        decisegundos = max(1, min(65535, decisegundos))
        # Largo en metros enteros. La densificación a 350 m garantiza que cabe
        # holgado en uint16.
        metros = max(1, min(65535, int(round(largo))))
        dirigidas.append((ia, ib, decisegundos, metros, grupo))
        if doble:
            dirigidas.append((ib, ia, decisegundos, metros, grupo))
        if grupo != GRUPO_NO_HABITABLE:
            habitable[ia] = True
            habitable[ib] = True

    # Marca de intersección: nodos con 3 o más vecinos distintos en la vista no
    # dirigida. Los nodos que quedaron de la densificación tienen 2 vecinos, así
    # que esto aísla los cruces de verdad, que es donde están los semáforos y
    # los discos pare. La demora por intersección es lo que explica por qué un
    # viaje de 4 km por Pedro de Valdivia toma más que 4 km de Costanera Norte.
    pares = np.array(
        [(indice_de[a], indice_de[b]) for a, b, *_ in aristas], dtype=np.int64
    )
    menor = pares.min(axis=1)
    mayor = pares.max(axis=1)
    # Se codifica cada arista no dirigida en un solo entero para poder deduplicar
    # con np.unique: una avenida de doble calzada aporta un vecino, no dos.
    aristas_unicas = np.unique(menor * n + mayor)
    grados = np.zeros(n, dtype=np.int32)
    np.add.at(grados, aristas_unicas // n, 1)
    np.add.at(grados, aristas_unicas % n, 1)
    es_juncion = (grados >= 3).astype(np.uint8)

    dirigidas.sort(key=lambda e: e[0])
    m = len(dirigidas)

    offsets = np.zeros(n + 1, dtype=np.int32)
    targets = np.empty(m, dtype=np.int32)
    base_ds = np.empty(m, dtype=np.uint16)
    largo_m = np.empty(m, dtype=np.uint16)
    grupos = np.empty(m, dtype=np.uint8)

    for pos, (ia, ib, ds, metros, grupo) in enumerate(dirigidas):
        targets[pos] = ib
        base_ds[pos] = ds
        largo_m[pos] = metros
        grupos[pos] = grupo
        offsets[ia + 1] += 1
    np.cumsum(offsets, out=offsets)

    return {
        "lats": lats,
        "lons": lons,
        "offsets": offsets,
        "targets": targets,
        "base_ds": base_ds,
        "largo_m": largo_m,
        "grupos": grupos,
        "juncion": es_juncion,
        "habitable": habitable,
        "indice_de": indice_de,
    }


# ---------------------------------------------------------------------------
# Grilla de candidatos para el centro de gravedad
# ---------------------------------------------------------------------------


def construir_grilla(grafo, mascara: MascaraArea, mascaras_por_comuna):
    # La comuna de Lo Barnechea llega hasta la frontera con Argentina y La Reina
    # se mete en la precordillera, así que el bounding box de los límites
    # comunales arrastra la grilla decenas de kilómetros hacia cerros sin
    # caminos. Se recorta contra el rectángulo del grafo: fuera de ahí no hay
    # red viaria y no se podría rutear igual.
    limites = mascara.bbox()
    bbox = {
        "lat_min": max(limites["lat_min"], GRAPH_BBOX["lat_min"]),
        "lat_max": min(limites["lat_max"], GRAPH_BBOX["lat_max"]),
        "lon_min": max(limites["lon_min"], GRAPH_BBOX["lon_min"]),
        "lon_max": min(limites["lon_max"], GRAPH_BBOX["lon_max"]),
    }
    lat_media = 0.5 * (bbox["lat_min"] + bbox["lat_max"])
    m_lat, m_lon = metros_por_grado(lat_media)
    paso_lat = GRID_SPACING_M / m_lat
    paso_lon = GRID_SPACING_M / m_lon

    # Índices enteros en vez de acumular floats: cada celda tiene fila y columna
    # exactas, que se exportan junto con ella para que el navegador pinte el
    # mapa de calor sin recalcular posiciones.
    ny = int((bbox["lat_max"] - bbox["lat_min"]) / paso_lat) + 1
    nx = int((bbox["lon_max"] - bbox["lon_min"]) / paso_lon) + 1

    print(
        f"  Grilla de {GRID_SPACING_M} m: {nx} x {ny} celdas sobre "
        f"{(bbox['lat_max'] - bbox['lat_min']) * m_lat / 1000:.0f} x "
        f"{(bbox['lon_max'] - bbox['lon_min']) * m_lon / 1000:.0f} km."
    )

    malla_iy, malla_ix = np.meshgrid(np.arange(ny), np.arange(nx), indexing="ij")
    malla_ix = malla_ix.ravel()
    malla_iy = malla_iy.ravel()
    todas_lat = bbox["lat_min"] + malla_iy * paso_lat
    todas_lon = bbox["lon_min"] + malla_ix * paso_lon

    dentro = mascara.dentro(todas_lat, todas_lon)
    print(f"    {int(dentro.sum()):,} celdas dentro de las comunas de análisis.")

    candidatas = np.nonzero(dentro)[0]
    lat_c = todas_lat[candidatas]
    lon_c = todas_lon[candidatas]

    # El índice se arma solo con nodos habitables: una celda cuyo único acceso
    # es la berma de una autopista no es un lugar donde se pueda vivir.
    habitables = np.nonzero(grafo["habitable"])[0]
    indice = IndiceNodos(grafo["lats"][habitables], grafo["lons"][habitables], celda_m=400.0)
    posiciones, _distancias = indice.mas_cercano_masivo(lat_c, lon_c, RADIO_SNAP_M)

    tiene_calle = posiciones >= 0
    print(
        f"    {int((~tiene_calle).sum()):,} descartadas por estar a más de "
        f"{RADIO_SNAP_M:.0f} m de una calle habitable (cerros, río, canchas de golf)."
    )

    utiles = np.nonzero(tiene_calle)[0]
    lat_u = lat_c[utiles]
    lon_u = lon_c[utiles]
    nodos_u = habitables[posiciones[utiles]].astype(np.int32)

    # Comuna de cada celda útil. El índice 0 queda reservado para "(fuera)".
    comunas_u = np.zeros(len(utiles), dtype=np.uint8)
    for indice_comuna, (_nombre, propia) in enumerate(mascaras_por_comuna, start=1):
        pendientes = comunas_u == 0
        if not pendientes.any():
            break
        posibles = np.nonzero(pendientes)[0]
        dentro_comuna = propia.dentro(lat_u[posibles], lon_u[posibles])
        comunas_u[posibles[dentro_comuna]] = indice_comuna

    print(f"  {len(utiles):,} celdas útiles en total.")

    return {
        "lats": lat_u,
        "lons": lon_u,
        "nodos": nodos_u,
        "comunas": comunas_u,
        "ix": malla_ix[candidatas][utiles].astype(np.uint16),
        "iy": malla_iy[candidatas][utiles].astype(np.uint16),
        "bbox": bbox,
        "nx": nx,
        "ny": ny,
        "paso_lat": paso_lat,
        "paso_lon": paso_lon,
    }


# ---------------------------------------------------------------------------
# Exportación
# ---------------------------------------------------------------------------


def _grados_a_int32(valores: np.ndarray) -> np.ndarray:
    """Grados a enteros escalados 1e7: ~1 cm de resolución, cabe en int32."""
    return np.round(valores * 1e7).astype(np.int32)


def exportar(grafo, grilla, nombres_comunas) -> None:
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    secciones = []
    buffer = bytearray()

    def agregar(nombre: str, array: np.ndarray) -> None:
        while len(buffer) % 4:  # alineación para los typed arrays de JS
            buffer.append(0)
        inicio = len(buffer)
        datos = array.tobytes()
        buffer.extend(datos)
        secciones.append((nombre, inicio, len(datos), str(array.dtype)))

    agregar("lat", _grados_a_int32(grafo["lats"]))
    agregar("lon", _grados_a_int32(grafo["lons"]))
    agregar("offsets", grafo["offsets"])
    agregar("targets", grafo["targets"])
    agregar("base_ds", grafo["base_ds"])
    agregar("largo_m", grafo["largo_m"])
    agregar("grupos", grafo["grupos"])
    agregar("juncion", grafo["juncion"])

    ruta_grafo = WEB_DATA_DIR / "graph.bin"
    ruta_grafo.write_bytes(bytes(buffer))

    buffer_grilla = bytearray()
    secciones_grilla = []

    def agregar_grilla(nombre: str, array: np.ndarray) -> None:
        while len(buffer_grilla) % 4:
            buffer_grilla.append(0)
        inicio = len(buffer_grilla)
        datos = array.tobytes()
        buffer_grilla.extend(datos)
        secciones_grilla.append((nombre, inicio, len(datos), str(array.dtype)))

    agregar_grilla("lat", _grados_a_int32(grilla["lats"]))
    agregar_grilla("lon", _grados_a_int32(grilla["lons"]))
    agregar_grilla("nodo", grilla["nodos"])
    agregar_grilla("comuna", grilla["comunas"])
    agregar_grilla("ix", grilla["ix"])
    agregar_grilla("iy", grilla["iy"])

    ruta_grilla = WEB_DATA_DIR / "grid.bin"
    ruta_grilla.write_bytes(bytes(buffer_grilla))

    meta = {
        "version": 1,
        "generado": datetime.now().isoformat(timespec="seconds"),
        "grafo": {
            "archivo": "graph.bin",
            "nodos": int(len(grafo["lats"])),
            "aristas": int(len(grafo["targets"])),
            "bbox": GRAPH_BBOX,
            "secciones": {
                nombre: {"offset": inicio, "bytes": largo, "dtype": dtype}
                for nombre, inicio, largo, dtype in secciones
            },
        },
        "grilla": {
            "archivo": "grid.bin",
            "celdas": int(len(grilla["nodos"])),
            "espaciado_m": GRID_SPACING_M,
            "bbox": grilla["bbox"],
            "nx": grilla["nx"],
            "ny": grilla["ny"],
            "paso_lat": grilla["paso_lat"],
            "paso_lon": grilla["paso_lon"],
            "comunas": nombres_comunas,
            "secciones": {
                nombre: {"offset": inicio, "bytes": largo, "dtype": dtype}
                for nombre, inicio, largo, dtype in secciones_grilla
            },
        },
        "grupos_via": GRUPOS,
    }

    (WEB_DATA_DIR / "graph_meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"\n  {ruta_grafo.name}: {len(buffer) / 1e6:.2f} MB")
    print(f"  {ruta_grilla.name}: {len(buffer_grilla) / 1e3:.0f} KB")
    print(f"  graph_meta.json escrito.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    print("Leyendo caché de OpenStreetMap...")
    datos = fetch_osm.fetch_vias(recargar=False)
    relaciones = fetch_osm.fetch_comunas(recargar=False)

    print("\nArmando máscara de la zona de análisis...")
    mascara = MascaraArea()
    nombres_comunas = ["(fuera)"]
    mascaras_por_comuna = []
    encontradas = []
    for relacion in relaciones:
        nombre = relacion.get("tags", {}).get("name", "")
        if nombre not in ANALYSIS_COMUNAS:
            continue
        anillos = mascara.agregar_relacion(relacion)
        if anillos == 0:
            print(f"  Aviso: no se pudo cerrar el borde de {nombre}.")
            continue
        propia = MascaraArea()
        propia.agregar_relacion(relacion)
        mascaras_por_comuna.append((nombre, propia))
        nombres_comunas.append(nombre)
        encontradas.append(nombre)

    faltantes = sorted(set(ANALYSIS_COMUNAS) - set(encontradas))
    if faltantes:
        print(f"  Aviso: no se encontraron los límites de {', '.join(faltantes)}.")

    for caja in ANALYSIS_EXTRA_BOXES:
        mascara.agregar_caja(caja)
        propia = MascaraArea()
        propia.agregar_caja(caja)
        mascaras_por_comuna.append((caja["nombre"], propia))
        nombres_comunas.append(caja["nombre"])
        print(f"  + rectángulo {caja['nombre']}")

    print("\nConstruyendo aristas...")
    aristas = construir_aristas(datos["ways"], datos["nodes"])
    print(f"  {len(aristas):,} aristas antes de filtrar componentes.")

    nodos_presentes = {nodo for arista in aristas for nodo in arista[:2]}
    conservados = componente_mayor(aristas, nodos_presentes)
    aristas = [a for a in aristas if a[0] in conservados and a[1] in conservados]

    print("\nArmando lista de adyacencia (CSR)...")
    grafo = construir_csr(aristas, datos["nodes"])
    print(
        f"  {len(grafo['lats']):,} nodos, {len(grafo['targets']):,} aristas dirigidas, "
        f"{int(grafo['habitable'].sum()):,} habitables, "
        f"{int(grafo['juncion'].sum()):,} intersecciones."
    )

    print("\nConstruyendo grilla de candidatos...")
    grilla = construir_grilla(grafo, mascara, mascaras_por_comuna)

    print("\nExportando...")
    exportar(grafo, grilla, nombres_comunas)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
