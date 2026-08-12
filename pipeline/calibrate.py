"""
Calibra el modelo de tráfico a partir de las mediciones manuales de Google Maps.

EL MODELO
---------
Para una ruta cualquiera y una franja horaria `b`:

    T_predicho = t0_b + Σ_c  ff_c · g_(c,b)

    ff_c   = minutos de flujo libre que la ruta pasa en vías del grupo c
             (autopista / arterial / local), que sale del grafo
    g_(c,b) = factor de demora de ese grupo en esa franja  (la incógnita)
    t0_b   = costo fijo del viaje: partir, estacionar, los primeros y últimos
             100 metros. También absorbe el sesgo sistemático del modelo.

Es **lineal en las incógnitas**, así que cada franja es una regresión chica e
independiente: 4 incógnitas (t0 + 3 factores) contra 8 mediciones. Por eso los
8 pares origen-destino se eligieron con composiciones muy distintas — si todos
fueran puro autopista, no habría forma de separar el factor de las arterias.

Tu intuición de "dame los extremos y deduce el resto" es exactamente esto: las
mediciones de madrugada fijan la escala base, las de punta fijan el techo, y el
modelo interpola cualquier ruta intermedia porque conoce la composición de vías
de cada una.

LA ITERACIÓN
------------
Hay un huevo-y-gallina: la composición `ff_c` depende de la ruta elegida, y la
ruta elegida depende de los factores de tráfico (en punta conviene la arteria,
en madrugada la autopista). Se resuelve iterando: rutear con los factores
actuales, reajustar, repetir. Converge en 2-3 vueltas.

Uso:
    python pipeline/calibrate.py              # ajusta con data/mediciones.csv
    python pipeline/calibrate.py --priors     # escribe solo los valores a priori
"""

import csv
import heapq
import itertools
import json
import sys
from datetime import datetime

import numpy as np

from config import (
    BUCKET_IDS,
    CALIBRATION_PAIRS,
    CLASS_GROUPS,
    DATA_DIR,
    TIME_BUCKETS,
    WEB_DATA_DIR,
)
from graph_io import cargar_grafo

GRUPOS = list(CLASS_GROUPS.keys())
N_GRUPOS = len(GRUPOS)

# Piso de los factores de demora. Nunca se va a andar más rápido que un 40% por
# debajo del tiempo de flujo libre modelado; el piso evita que una medición
# rara empuje el ajuste a velocidades absurdas.
G_MINIMO = 0.4

# Valores a priori. NO son datos medidos: son estimaciones con la forma de un
# día típico de Santiago — dos puntas, la de la tarde peor que la de la mañana,
# y un valle de mediodía que igual va cargado. Sirven para dos cosas: como
# semilla de la iteración de ruteo, y para que la app funcione antes de tener
# mediciones. Están deliberadamente por lo bajo en las puntas, así que hasta
# que se cargue data/mediciones.csv el modelo va a ser optimista.
#
# El índice de tráfico de TomTom para Santiago
# (https://www.tomtom.com/traffic-index/city/santiago/) es un buen contraste
# para revisar la forma de la curva, pero la magnitud la fijan las mediciones.
PRIORS = {
    #              t0(s)  autopista  arterial  local
    "madrugada": (60, 0.95, 0.95, 1.00),
    "punta_am": (180, 1.55, 1.45, 1.20),
    "valle": (120, 1.15, 1.25, 1.10),
    "punta_pm": (200, 1.70, 1.55, 1.25),
    "noche": (90, 1.00, 1.10, 1.05),
}

# Peso de la regularización que tira el ajuste hacia los priors. Bajo: las
# mediciones mandan, el prior solo desempata cuando los datos no alcanzan a
# identificar un factor.
LAMBDA_PRIOR = 0.35


# ---------------------------------------------------------------------------
# Ruteo
# ---------------------------------------------------------------------------


def dijkstra_hasta(grafo, origen: int, destino: int, factores: np.ndarray):
    """
    Camino más rápido de `origen` a `destino` con los factores dados.

    Devuelve (tiempo_total_s, ff_por_grupo_s) donde ff_por_grupo_s es el tiempo
    de FLUJO LIBRE acumulado en cada grupo de vía a lo largo de la ruta óptima.
    Esa descomposición es la fila de la matriz de diseño de la regresión.
    """
    n = grafo.n_nodos
    INF = float("inf")
    dist = np.full(n, INF)
    dist[origen] = 0.0
    # Arista por la que se llegó a cada nodo, para reconstruir la ruta.
    arista_previa = np.full(n, -1, dtype=np.int64)
    nodo_previo = np.full(n, -1, dtype=np.int64)
    visitado = np.zeros(n, dtype=bool)

    offsets = grafo.offsets
    targets = grafo.targets
    base_ds = grafo.base_ds
    grupos = grafo.grupos

    cola = [(0.0, origen)]
    while cola:
        d, u = heapq.heappop(cola)
        if visitado[u]:
            continue
        visitado[u] = True
        if u == destino:
            break
        for arista in range(offsets[u], offsets[u + 1]):
            v = int(targets[arista])
            if visitado[v]:
                continue
            peso = (base_ds[arista] / 10.0) * factores[grupos[arista]]
            nueva = d + peso
            if nueva < dist[v]:
                dist[v] = nueva
                arista_previa[v] = arista
                nodo_previo[v] = u
                heapq.heappush(cola, (nueva, v))

    if not visitado[destino]:
        return None, None

    ff_por_grupo = np.zeros(N_GRUPOS)
    nodo = destino
    while nodo != origen:
        arista = arista_previa[nodo]
        if arista < 0:
            return None, None
        ff_por_grupo[grupos[arista]] += base_ds[arista] / 10.0
        nodo = int(nodo_previo[nodo])

    return float(dist[destino]), ff_por_grupo


# ---------------------------------------------------------------------------
# Mínimos cuadrados con restricciones
# ---------------------------------------------------------------------------


def nnls_exacta(A: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    Mínimos cuadrados no negativos por enumeración de conjuntos activos.

    Con 4 incógnitas hay solo 16 subconjuntos posibles de variables no nulas.
    Se resuelve el problema sin restricciones en cada uno y se queda el mejor
    que resulte factible: el óptimo global, sin iteraciones que puedan quedar
    dando vueltas.
    """
    n = A.shape[1]
    mejor_x = np.zeros(n)
    mejor_residuo = float(np.sum(b**2))

    for tamano in range(1, n + 1):
        for soporte in itertools.combinations(range(n), tamano):
            indices = list(soporte)
            solucion, *_ = np.linalg.lstsq(A[:, indices], b, rcond=None)
            if np.any(solucion < -1e-9):
                continue
            candidato = np.zeros(n)
            candidato[indices] = np.maximum(solucion, 0.0)
            residuo = float(np.sum((A @ candidato - b) ** 2))
            if residuo < mejor_residuo - 1e-12:
                mejor_residuo, mejor_x = residuo, candidato

    return mejor_x


def ajustar_franja(filas, prior) -> dict:
    """
    Ajusta t0 y los 3 factores de una franja.

    Cambio de variable para pasar de "g >= G_MINIMO" a "u >= 0":
        g_c = G_MINIMO + u_c,   t0 = u_0
    con lo que la ecuación queda
        T_obs - G_MINIMO · Σ ff_c  =  u_0 + Σ ff_c · u_c
    que es un problema no negativo puro.
    """
    t0_prior, *g_prior = prior

    A = []
    b = []
    for ff, observado_s in filas:
        A.append([1.0] + list(ff))
        b.append(observado_s - G_MINIMO * float(np.sum(ff)))

    # Filas de regularización: empujan cada incógnita hacia su prior.
    escala = np.mean([np.sum(ff) for ff, _ in filas]) or 1.0
    for indice in range(1 + N_GRUPOS):
        fila = np.zeros(1 + N_GRUPOS)
        fila[indice] = LAMBDA_PRIOR * escala
        A.append(fila.tolist())
        objetivo = t0_prior if indice == 0 else (g_prior[indice - 1] - G_MINIMO)
        b.append(LAMBDA_PRIOR * escala * objetivo)

    u = nnls_exacta(np.array(A, dtype=np.float64), np.array(b, dtype=np.float64))

    t0 = float(u[0])
    factores = [G_MINIMO + float(valor) for valor in u[1:]]

    # Residuos solo sobre las mediciones reales, sin las filas de regularización.
    predichos = [t0 + float(np.dot(ff, factores)) for ff, _ in filas]
    observados = [obs for _, obs in filas]
    errores = np.array(predichos) - np.array(observados)

    return {
        "t0_s": round(t0, 1),
        "g": [round(valor, 4) for valor in factores],
        "n": len(filas),
        "rmse_min": round(float(np.sqrt(np.mean(errores**2))) / 60.0, 2),
        "error_max_min": round(float(np.max(np.abs(errores))) / 60.0, 2),
    }


# ---------------------------------------------------------------------------
# Lectura de mediciones
# ---------------------------------------------------------------------------


def leer_mediciones() -> dict[str, list[tuple[str, float]]]:
    """Devuelve {franja: [(id_par, minutos_observados), ...]} solo con filas llenas."""
    ruta = DATA_DIR / "mediciones.csv"
    if not ruta.exists():
        return {}

    por_franja: dict[str, list[tuple[str, float]]] = {}
    with ruta.open(encoding="utf-8-sig", newline="") as fh:
        for fila in csv.DictReader(fh, delimiter=";"):
            franja = (fila.get("franja") or "").strip()
            id_par = (fila.get("id_par") or "").strip()
            if franja not in BUCKET_IDS or not id_par:
                continue

            minutos = _a_float(fila.get("minutos"))
            if minutos is None:
                lo = _a_float(fila.get("minutos_min"))
                hi = _a_float(fila.get("minutos_max"))
                if lo is not None and hi is not None:
                    minutos = 0.5 * (lo + hi)
                elif lo is not None or hi is not None:
                    minutos = lo if lo is not None else hi
            if minutos is None or minutos <= 0:
                continue

            por_franja.setdefault(franja, []).append((id_par, minutos))

    return por_franja


def _a_float(valor) -> float | None:
    if valor is None:
        return None
    texto = str(valor).strip().replace(",", ".")
    if not texto:
        return None
    try:
        return float(texto)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Escritura del resultado
# ---------------------------------------------------------------------------


def escribir_traffic_json(por_franja: dict, fuente: str, diagnostico: dict) -> None:
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    franjas = []
    for bucket in TIME_BUCKETS:
        ajuste = por_franja[bucket["id"]]
        franjas.append(
            {
                "id": bucket["id"],
                "label": bucket["label"],
                "hora_referencia": f'{bucket["dia"]} {bucket["hora"]}',
                "t0_s": ajuste["t0_s"],
                "g": ajuste["g"],
            }
        )

    salida = {
        "version": 1,
        "generado": datetime.now().isoformat(timespec="seconds"),
        "fuente": fuente,
        "grupos": GRUPOS,
        "g_minimo": G_MINIMO,
        "franjas": franjas,
        "diagnostico": diagnostico,
    }
    ruta = WEB_DATA_DIR / "traffic.json"
    ruta.write_text(json.dumps(salida, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nEscrito {ruta}")


def solo_priors() -> int:
    por_franja = {
        franja: {
            "t0_s": float(prior[0]),
            "g": [round(valor, 4) for valor in prior[1:]],
            "n": 0,
            "rmse_min": None,
            "error_max_min": None,
        }
        for franja, prior in PRIORS.items()
    }
    escribir_traffic_json(
        por_franja,
        fuente="valores a priori estimados, sin mediciones (el modelo es optimista en las puntas)",
        diagnostico={"nota": "Sin mediciones cargadas. Llena data/mediciones.csv y vuelve a correr."},
    )
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    if "--priors" in sys.argv:
        return solo_priors()

    mediciones = leer_mediciones()
    total = sum(len(v) for v in mediciones.values())
    if total == 0:
        print("data/mediciones.csv está vacío. Escribiendo solo los valores a priori.")
        return solo_priors()

    print(f"{total} mediciones cargadas:")
    for franja in BUCKET_IDS:
        print(f"  {franja:12s} {len(mediciones.get(franja, [])):2d}")

    grafo = cargar_grafo()
    print(f"\nGrafo: {grafo.n_nodos:,} nodos, {grafo.n_aristas:,} aristas.")

    pares = {par["id"]: par for par in CALIBRATION_PAIRS}
    nodos = {}
    for id_par, par in pares.items():
        nodos[id_par] = (
            grafo.nodo_mas_cercano(*par["origen_latlon"]),
            grafo.nodo_mas_cercano(*par["destino_latlon"]),
        )

    resultados = {franja: PRIORS[franja] for franja in BUCKET_IDS}
    ajustes: dict[str, dict] = {}
    detalle: dict[str, list] = {}

    ITERACIONES = 3
    for iteracion in range(1, ITERACIONES + 1):
        print(f"\n--- Iteración {iteracion}/{ITERACIONES} ---")
        for franja in BUCKET_IDS:
            observaciones = mediciones.get(franja, [])
            if not observaciones:
                ajustes[franja] = {
                    "t0_s": float(PRIORS[franja][0]),
                    "g": [round(v, 4) for v in PRIORS[franja][1:]],
                    "n": 0,
                    "rmse_min": None,
                    "error_max_min": None,
                }
                continue

            factores_actuales = np.array(resultados[franja][1:], dtype=np.float64)

            filas = []
            filas_detalle = []
            for id_par, minutos in observaciones:
                if id_par not in nodos:
                    print(f"  Aviso: par desconocido '{id_par}', se ignora.")
                    continue
                origen, destino = nodos[id_par]
                _tiempo, ff = dijkstra_hasta(grafo, origen, destino, factores_actuales)
                if ff is None:
                    print(f"  Aviso: no hay ruta para '{id_par}', se ignora.")
                    continue
                filas.append((ff, minutos * 60.0))
                filas_detalle.append((id_par, minutos, ff))

            if len(filas) < 2:
                print(f"  {franja}: muy pocas mediciones utilizables, se deja el prior.")
                continue

            ajuste = ajustar_franja(filas, PRIORS[franja])
            ajustes[franja] = ajuste
            resultados[franja] = (ajuste["t0_s"], *ajuste["g"])
            detalle[franja] = filas_detalle

            factores_txt = "  ".join(
                f"{nombre}={valor:.2f}" for nombre, valor in zip(GRUPOS, ajuste["g"])
            )
            print(
                f"  {franja:12s} t0={ajuste['t0_s']:5.0f}s  {factores_txt}"
                f"   rmse={ajuste['rmse_min']:.2f} min  (n={ajuste['n']})"
            )

    print("\n--- Ajuste final por medición ---")
    diagnostico_pares = []
    for franja in BUCKET_IDS:
        if franja not in detalle:
            continue
        t0 = ajustes[franja]["t0_s"]
        factores = ajustes[franja]["g"]
        for id_par, observado, ff in detalle[franja]:
            predicho = (t0 + float(np.dot(ff, factores))) / 60.0
            diagnostico_pares.append(
                {
                    "franja": franja,
                    "par": id_par,
                    "observado_min": round(observado, 1),
                    "predicho_min": round(predicho, 1),
                    "error_min": round(predicho - observado, 1),
                }
            )
            marca = "!" if abs(predicho - observado) > 5 else " "
            print(
                f" {marca} {franja:12s} {id_par:22s} obs={observado:5.1f}  "
                f"pred={predicho:5.1f}  err={predicho - observado:+5.1f} min"
            )

    errores = [d["error_min"] for d in diagnostico_pares]
    rmse_global = float(np.sqrt(np.mean(np.square(errores)))) if errores else None
    print(f"\nRMSE global: {rmse_global:.2f} min sobre {len(errores)} mediciones.")

    escribir_traffic_json(
        ajustes,
        fuente=f"{total} mediciones de Google Maps + priors TomTom",
        diagnostico={
            "rmse_global_min": round(rmse_global, 2) if rmse_global else None,
            "mediciones": diagnostico_pares,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
