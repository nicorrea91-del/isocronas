"""
Configuración central del Proyecto Isócronas.

Todo lo que se ajusta a mano vive acá: el área geográfica, las velocidades de
flujo libre por tipo de vía, las franjas horarias y los pares origen-destino
que se usan para calibrar el tráfico.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"
DATA_DIR = ROOT / "data"
WEB_DATA_DIR = ROOT / "web" / "data"


# ---------------------------------------------------------------------------
# Área geográfica
# ---------------------------------------------------------------------------

# Rectángulo de la RED VIARIA que se descarga y sobre la que se rutea.
# Es deliberadamente más grande que la zona de análisis: los viajes entre
# Chicureo y Las Condes pasan por Colina rural, Quilicura, Huechuraba y
# Recoleta (Los Libertadores -> Vespucio Norte -> Costanera Norte). Si se
# recorta el grafo a las comunas de interés, el ruteo inventa desvíos.
GRAPH_BBOX = {
    "lat_min": -33.52,
    "lat_max": -33.15,
    "lon_min": -70.82,
    "lon_max": -70.44,
}

# Zona de ANÁLISIS: dónde se busca el centro de gravedad y dónde se dibuja el
# mapa de calor. Se arma con los límites administrativos de OSM de estas
# comunas, más los polígonos manuales de abajo.
ANALYSIS_COMUNAS = [
    "Lo Barnechea",
    "Las Condes",
    "Vitacura",
    "Providencia",
    "La Reina",
    "Ñuñoa",
]

# Colina completa incluye mucha zona rural que no interesa. En vez del límite
# comunal, se usan estos rectángulos alrededor de los sectores urbanos.
ANALYSIS_EXTRA_BOXES = [
    {
        "nombre": "Chicureo - Chamisero",
        "lat_min": -33.325,
        "lat_max": -33.255,
        "lon_min": -70.710,
        "lon_max": -70.565,
    },
]

# Resolución de la grilla de candidatos para el centro de gravedad, en metros.
# A 150 m quedan unas 20.000 celdas útiles: detalle de manzana en el mapa de
# calor, y recombinarlas al mover un slider sigue siendo instantáneo.
GRID_SPACING_M = 150


# ---------------------------------------------------------------------------
# Red viaria: qué se incluye y a qué velocidad se circula sin tráfico
# ---------------------------------------------------------------------------

# Velocidades de flujo libre en km/h (madrugada, calle despejada). Son el
# punto de partida; la calibración de tráfico las corrige por franja horaria.
FREEFLOW_KMH = {
    "motorway": 100,
    "motorway_link": 60,
    "trunk": 80,
    "trunk_link": 50,
    "primary": 60,
    "primary_link": 40,
    "secondary": 50,
    "secondary_link": 35,
    "tertiary": 40,
    "tertiary_link": 30,
    "unclassified": 30,
    "residential": 30,
    "living_street": 15,
}

# Se excluye `service` (pasillos de estacionamiento, accesos privados):
# multiplica el tamaño del grafo sin aportar al ruteo real.
INCLUDED_HIGHWAYS = tuple(FREEFLOW_KMH.keys())

# Agrupación de tipos de vía para calibrar tráfico. Con 40 mediciones no se
# pueden estimar 13 factores por franja; con 3 grupos el ajuste es sólido.
CLASS_GROUPS = {
    "autopista": ["motorway", "motorway_link", "trunk", "trunk_link"],
    "arterial": ["primary", "primary_link", "secondary", "secondary_link"],
    "local": [
        "tertiary",
        "tertiary_link",
        "unclassified",
        "residential",
        "living_street",
    ],
}

GROUP_OF_HIGHWAY = {
    hw: group for group, hws in CLASS_GROUPS.items() for hw in hws
}


# ---------------------------------------------------------------------------
# Franjas horarias
# ---------------------------------------------------------------------------

# `madrugada` es la referencia de flujo libre: su factor de demora se fija
# en 1.0 y ancla la escala de todas las demás.
TIME_BUCKETS = [
    {
        "id": "madrugada",
        "label": "Madrugada (referencia sin tráfico)",
        "dia": "martes",
        "hora": "03:00",
        "es_referencia": True,
    },
    {
        "id": "punta_am",
        "label": "Punta mañana",
        "dia": "martes",
        "hora": "08:00",
        "es_referencia": False,
    },
    {
        "id": "valle",
        "label": "Valle / horario normal",
        "dia": "martes",
        "hora": "13:00",
        "es_referencia": False,
    },
    {
        "id": "punta_pm",
        "label": "Punta tarde",
        "dia": "martes",
        "hora": "18:30",
        "es_referencia": False,
    },
    {
        "id": "noche",
        "label": "Noche",
        "dia": "martes",
        "hora": "21:30",
        "es_referencia": False,
    },
]

BUCKET_IDS = [b["id"] for b in TIME_BUCKETS]
REFERENCE_BUCKET = next(b["id"] for b in TIME_BUCKETS if b["es_referencia"])


# ---------------------------------------------------------------------------
# Pares origen-destino para calibrar
# ---------------------------------------------------------------------------

# Elegidos para que la composición de tipos de vía varíe harto entre rutas:
# si todas fueran puro autopista, no se podría separar el factor de las
# arterias del de las autopistas. Las coordenadas son aproximadas al cruce
# indicado; corrígelas si Google Maps te toma un punto muy distinto.
CALIBRATION_PAIRS = [
    {
        "id": "chicureo_elgolf",
        "origen": "Rotonda Chicureo, Colina",
        "origen_latlon": (-33.2965, -70.6510),
        "destino": "Isidora Goyenechea con Augusto Leguía, Las Condes",
        "destino_latlon": (-33.4143, -70.5960),
        "por_que": "Corredor largo de autopista, sentido entrante",
    },
    {
        "id": "elgolf_chicureo",
        "origen": "Isidora Goyenechea con Augusto Leguía, Las Condes",
        "origen_latlon": (-33.4143, -70.5960),
        "destino": "Rotonda Chicureo, Colina",
        "destino_latlon": (-33.2965, -70.6510),
        "por_que": "Mismo corredor al revés: mide la asimetría de la punta",
    },
    {
        "id": "ladehesa_losleones",
        "origen": "Av. La Dehesa con El Rodeo, Lo Barnechea",
        "origen_latlon": (-33.3620, -70.5185),
        "destino": "Av. Providencia con Los Leones, Providencia",
        "destino_latlon": (-33.4262, -70.6100),
        "por_que": "Mezcla de expresa (Kennedy/Costanera) y arterias",
    },
    {
        "id": "nunoa_apoquindo",
        "origen": "Plaza Ñuñoa",
        "origen_latlon": (-33.4560, -70.5980),
        "destino": "Apoquindo con Manquehue, Las Condes",
        "destino_latlon": (-33.4090, -70.5720),
        "por_que": "Casi todo arterial urbano, sin autopista",
    },
    {
        "id": "lareina_vitacura",
        "origen": "Larraín con Tobalaba, La Reina",
        "origen_latlon": (-33.4520, -70.5570),
        "destino": "Alonso de Córdova con Vitacura, Vitacura",
        "destino_latlon": (-33.3970, -70.5820),
        "por_que": "Arterial + local, transversal oriente",
    },
    {
        "id": "chamisero_colina",
        "origen": "Chamisero, Colina",
        # Coordenada del pueblo según Nominatim. La primera estimación estaba 6 km
        # al noreste, caía a 446 m del camino más cercano y hacía que el modelo
        # ruteara con un desvío de 1,98x, sobreestimando el par en 6-18 min en
        # todas las franjas y arrastrando hacia abajo el ajuste completo.
        "origen_latlon": (-33.3218, -70.6476),
        "destino": "Plaza de Colina",
        "destino_latlon": (-33.2030, -70.6740),
        "por_que": "Caminos semiurbanos y rurales, poco congestionados",
    },
    {
        "id": "providencia_nunoa",
        "origen": "Pedro de Valdivia con Providencia",
        "origen_latlon": (-33.4270, -70.6060),
        "destino": "Irarrázaval con Pedro de Valdivia, Ñuñoa",
        "destino_latlon": (-33.4560, -70.6020),
        "por_que": "Viaje corto: detecta el peso de semáforos y calle local",
    },
    {
        "id": "lobarnechea_kennedy",
        "origen": "Av. Lo Barnechea con El Rodeo",
        "origen_latlon": (-33.3505, -70.5220),
        "destino": "Kennedy con Manquehue (Parque Arauco)",
        "destino_latlon": (-33.4020, -70.5760),
        "por_que": "Bajada de Lo Barnechea, cuello de botella conocido",
    },
]


# ---------------------------------------------------------------------------
# Ponderación de viajes
# ---------------------------------------------------------------------------

# Máximo de viajes semanales por punto: 14 = 2 viajes diarios los 7 días.
MAX_TRIPS_PER_WEEK = 14
