"""
Calibra el modelo de tráfico a partir de las mediciones manuales de Google Maps.

EL MODELO
---------
Para una ruta cualquiera y una franja horaria `b`:

    T_predicho = t0_b + Σ_c ff_c · g_(c,b) + N · d_b

    ff_c   = minutos de flujo libre que la ruta pasa en vías del grupo c
             (autopista / arterial / local), que sale del grafo
    g_(c,b) = factor de demora de ese grupo en esa franja
    N      = cantidad de intersecciones que atraviesa la ruta
    d_b    = demora media por intersección en esa franja
    t0_b   = costo fijo del viaje: partir, estacionar, los primeros y últimos
             100 metros. También absorbe el sesgo sistemático del modelo.

El término de intersecciones es lo que hace que el modelo sirva. Un modelo
puramente multiplicativo no puede describir a la vez un viaje de 4 km por Pedro
de Valdivia y uno de 22 km por Costanera Norte: el primero exige un factor
arterial de 6 y el segundo de 3. La causa física de la diferencia no es el tipo
de vía sino la **densidad de semáforos**, y contar intersecciones la captura
directamente sin romper la linealidad del ajuste.

Son 5 incógnitas por franja (t0 + 3 factores + demora por intersección) contra 8
mediciones, así que cada franja es una regresión chica e independiente. Los 8
pares origen-destino se eligieron con composiciones muy distintas a propósito:
si todos fueran puro autopista, no habría forma de separar el factor de las
arterias del de las autopistas.

LA ITERACIÓN
------------
Hay un huevo-y-gallina: la composición de la ruta depende de los parámetros, y
los parámetros dependen de la ruta. Se resuelve iterando, con amortiguación para
que no oscile, y se queda el ajuste con menor error de todas las vueltas.

ROBUSTEZ
--------
Se reponderan las mediciones estilo Huber: la que el modelo no logra explicar
pesa menos, para que un par con una coordenada mala o una ruta que Google toma
distinta no arrastre el ajuste completo. Los pares castigados se reportan.

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
# Incógnitas por franja: t0, un factor por grupo de vía, y la demora por cruce.
N_INCOGNITAS = 1 + N_GRUPOS + 1

# Cajas de cada parámetro. NO son un detalle: sin cota superior en el costo fijo,
# el ajuste cambia toda la estructura multiplicativa por una constante enorme
# (llegó a 20 min por viaje) y clava los factores en su piso. Eso da un RMSE
# bajísimo en las 8 mediciones y un modelo inservible: un candidato a 500 m del
# destino quedaría predicho en 20 minutos.
#
# El costo fijo real de un viaje —caminar al auto, salir, estacionar— son unos
# pocos minutos, y ahí está acotado.
COTAS = {
    "t0_s": (30.0, 240.0),
    "g": (0.7, 6.0),
    "d_cruce_s": (0.0, 60.0),
}

# Los enlaces de autopista son a distinto nivel: no se espera en ellos. Contar
# los nodos de grado 3 de una autopista como semáforos obligaba al factor de
# autopista contra su piso.
GRUPO_SIN_ESPERA = GRUPOS.index("autopista")

# Valores a priori. NO son datos medidos: son estimaciones con la forma de un
# día típico de Santiago — dos puntas, la de la tarde peor que la de la mañana.
# Sirven como semilla de la iteración, como desempate cuando las mediciones no
# alcanzan a identificar un parámetro, y para que la app funcione antes de tener
# mediciones. El índice de tráfico de TomTom para Santiago
# (https://www.tomtom.com/traffic-index/city/santiago/) es un buen contraste
# para la forma de la curva, pero la magnitud la fijan las mediciones.
PRIORS = {
    #              t0(s)  autopista  arterial  local  demora_cruce(s)
    "madrugada": (60, 1.00, 1.00, 1.00, 8),
    "punta_am": (150, 1.35, 1.30, 1.15, 30),
    "valle": (110, 1.10, 1.15, 1.05, 18),
    "punta_pm": (160, 1.45, 1.40, 1.20, 33),
    "noche": (80, 1.00, 1.05, 1.00, 12),
}

# Peso de la regularización hacia los priors, medido en "fracción de una
# observación extra". Bajo a propósito: con 8 mediciones por franja los datos
# tienen que mandar, y los priors son estimaciones optimistas.
#
# Se escala por columna. En una versión anterior se usaba una sola escala para
# todas, y como la columna de t0 tiene coeficiente 1 en los datos y la de los
# tiempos de vía tiene cientos, t0 quedaba clavado exactamente en su prior.
ALFA_PRIOR = 0.15

# Umbral de Huber para la reponderación robusta.
HUBER_S = 240.0
ITERACIONES_ROBUSTAS = 3

# Vueltas de la iteración ruta -> ajuste -> ruta, y cuánto se cree del ajuste
# nuevo al volver a rutear (el resto se mantiene del anterior).
ITERACIONES_RUTA = 5
AMORTIGUACION = 0.6


# ---------------------------------------------------------------------------
# Ruteo
# ---------------------------------------------------------------------------


def dijkstra_hasta(grafo, origen: int, destino: int, factores: np.ndarray, demora_cruce: float):
    """
    Camino más rápido de `origen` a `destino` con los parámetros dados.

    Devuelve (tiempo_total_s, ff_por_grupo_s, n_intersecciones). Esa
    descomposición es exactamente la fila de la matriz de diseño.
    """
    n = grafo.n_nodos
    dist = np.full(n, np.inf)
    dist[origen] = 0.0
    arista_previa = np.full(n, -1, dtype=np.int64)
    nodo_previo = np.full(n, -1, dtype=np.int64)
    visitado = np.zeros(n, dtype=bool)

    offsets = grafo.offsets
    targets = grafo.targets
    base_ds = grafo.base_ds
    grupos = grafo.grupos
    juncion = grafo.juncion

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
            if juncion[v] and grupos[arista] != GRUPO_SIN_ESPERA:
                peso += demora_cruce
            nueva = d + peso
            if nueva < dist[v]:
                dist[v] = nueva
                arista_previa[v] = arista
                nodo_previo[v] = u
                heapq.heappush(cola, (nueva, v))

    if not visitado[destino]:
        return None, None, None

    ff_por_grupo = np.zeros(N_GRUPOS)
    cruces = 0
    nodo = destino
    while nodo != origen:
        arista = arista_previa[nodo]
        if arista < 0:
            return None, None, None
        ff_por_grupo[grupos[arista]] += base_ds[arista] / 10.0
        # El destino no cuenta como cruce que haya que esperar: se llega y ya.
        # Y los cruces alcanzados por autopista son enlaces, no semáforos.
        if nodo != destino and juncion[nodo] and grupos[arista] != GRUPO_SIN_ESPERA:
            cruces += 1
        nodo = int(nodo_previo[nodo])

    return float(dist[destino]), ff_por_grupo, cruces


# ---------------------------------------------------------------------------
# Mínimos cuadrados no negativos
# ---------------------------------------------------------------------------


def lsq_con_cajas(A: np.ndarray, b: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """
    Mínimos cuadrados con cada incógnita acotada a un intervalo.

    El óptimo de un problema así siempre vive en alguna cara de la caja: cada
    variable queda pegada a su piso, pegada a su techo, o en el interior
    resolviendo los mínimos cuadrados sin restricciones sobre las demás. Con 5
    incógnitas hay 3^5 = 243 caras, así que se enumeran todas y se toma la de
    menor residuo entre las factibles. Es el óptimo global y no hay iteraciones
    que puedan quedar oscilando.
    """
    n = A.shape[1]
    mejor_x, mejor_residuo = None, np.inf

    for estados in itertools.product((0, 1, 2), repeat=n):
        x = np.empty(n)
        for j, estado in enumerate(estados):
            x[j] = lo[j] if estado == 0 else (hi[j] if estado == 2 else 0.0)

        libres = [j for j in range(n) if estados[j] == 1]
        if libres:
            fijas = np.array([0.0 if estados[j] == 1 else x[j] for j in range(n)])
            solucion, *_ = np.linalg.lstsq(A[:, libres], b - A @ fijas, rcond=None)
            if np.any(solucion < lo[libres] - 1e-9) or np.any(solucion > hi[libres] + 1e-9):
                continue
            x[libres] = solucion

        residuo = float(np.sum((A @ x - b) ** 2))
        if residuo < mejor_residuo:
            mejor_residuo, mejor_x = residuo, x

    return mejor_x if mejor_x is not None else np.clip(np.zeros(n), lo, hi)


def _cajas() -> tuple[np.ndarray, np.ndarray]:
    lo = np.empty(N_INCOGNITAS)
    hi = np.empty(N_INCOGNITAS)
    lo[0], hi[0] = COTAS["t0_s"]
    lo[1 : 1 + N_GRUPOS], hi[1 : 1 + N_GRUPOS] = COTAS["g"]
    lo[1 + N_GRUPOS], hi[1 + N_GRUPOS] = COTAS["d_cruce_s"]
    return lo, hi


def _matriz_de_diseno(filas):
    """
    Cada medición es una fila:

        T_obs  =  t0 · 1  +  Σ_c ff_c · g_c  +  N · d

    Las incógnitas van en sus unidades naturales, acotadas por COTAS.
    """
    A = np.empty((len(filas), N_INCOGNITAS))
    b = np.empty(len(filas))
    for i, (ff, cruces, observado_s) in enumerate(filas):
        A[i, 0] = 1.0
        A[i, 1 : 1 + N_GRUPOS] = ff
        A[i, 1 + N_GRUPOS] = cruces
        b[i] = observado_s
    return A, b


def _objetivo_prior(prior) -> np.ndarray:
    return np.array(prior, dtype=np.float64)


def ajustar_franja(filas, prior) -> dict:
    """Ajusta t0, los factores por grupo de vía y la demora por intersección."""
    A_datos, b_datos = _matriz_de_diseno(filas)
    objetivo = _objetivo_prior(prior)
    lo, hi = _cajas()
    n_filas = len(filas)

    # Regularización escalada POR COLUMNA: cada fila de penalización pesa una
    # fracción ALFA_PRIOR de una observación típica de esa misma columna.
    escalas = np.linalg.norm(A_datos, axis=0) / np.sqrt(n_filas)
    escalas[escalas == 0] = 1.0
    pesos_prior = ALFA_PRIOR * escalas
    A_prior = np.diag(pesos_prior)
    b_prior = pesos_prior * objetivo

    pesos = np.ones(n_filas)
    x = np.clip(objetivo, lo, hi)

    for _ in range(ITERACIONES_ROBUSTAS):
        raiz = np.sqrt(pesos)
        A = np.vstack([A_datos * raiz[:, None], A_prior])
        b = np.concatenate([b_datos * raiz, b_prior])
        x = lsq_con_cajas(A, b, lo, hi)

        # Reponderación de Huber sobre los residuos en segundos.
        residuos = A_datos @ x - b_datos
        pesos = np.minimum(1.0, HUBER_S / np.maximum(1e-6, np.abs(residuos)))

    t0 = float(x[0])
    factores = [float(v) for v in x[1 : 1 + N_GRUPOS]]
    demora_cruce = float(x[1 + N_GRUPOS])

    residuos = A_datos @ x - b_datos
    return {
        "t0_s": round(t0, 1),
        "g": [round(v, 4) for v in factores],
        "d_cruce_s": round(demora_cruce, 2),
        "n": n_filas,
        "rmse_min": round(float(np.sqrt(np.mean(residuos**2))) / 60.0, 2),
        "error_max_min": round(float(np.max(np.abs(residuos))) / 60.0, 2),
        "pesos": pesos.tolist(),
    }


def _parametros_de_ruteo(ajuste):
    return np.array(ajuste["g"], dtype=np.float64), float(ajuste["d_cruce_s"])


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
                "d_cruce_s": ajuste["d_cruce_s"],
            }
        )

    salida = {
        "version": 2,
        "generado": datetime.now().isoformat(timespec="seconds"),
        "fuente": fuente,
        "grupos": GRUPOS,
        "cotas": {llave: list(valor) for llave, valor in COTAS.items()},
        # Los cruces alcanzados por una arista de autopista son enlaces a
        # distinto nivel y no llevan demora. El navegador aplica la misma regla.
        "grupo_sin_espera": GRUPOS[GRUPO_SIN_ESPERA],
        "franjas": franjas,
        "diagnostico": diagnostico,
    }
    ruta = WEB_DATA_DIR / "traffic.json"
    ruta.write_text(json.dumps(salida, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nEscrito {ruta}")


def solo_priors() -> int:
    por_franja = {}
    for franja, prior in PRIORS.items():
        objetivo = prior
        por_franja[franja] = {
            "t0_s": float(objetivo[0]),
            "g": [round(v, 4) for v in objetivo[1 : 1 + N_GRUPOS]],
            "d_cruce_s": float(objetivo[1 + N_GRUPOS]),
            "n": 0,
            "rmse_min": None,
            "error_max_min": None,
        }
    escribir_traffic_json(
        por_franja,
        fuente="valores a priori estimados, sin mediciones (el modelo es optimista en las puntas)",
        diagnostico={
            "nota": "Sin mediciones cargadas. Llena data/mediciones.csv y vuelve a correr."
        },
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

    print(f"{total} mediciones cargadas: ", end="")
    print(", ".join(f"{f}={len(mediciones.get(f, []))}" for f in BUCKET_IDS))

    grafo = cargar_grafo()
    print(
        f"Grafo: {grafo.n_nodos:,} nodos, {grafo.n_aristas:,} aristas, "
        f"{int(grafo.juncion.sum()):,} intersecciones."
    )

    pares = {par["id"]: par for par in CALIBRATION_PAIRS}
    nodos = {}
    for id_par, par in pares.items():
        nodos[id_par] = (
            grafo.nodo_mas_cercano(*par["origen_latlon"]),
            grafo.nodo_mas_cercano(*par["destino_latlon"]),
        )

    # Estado de ruteo por franja, y el mejor ajuste visto.
    ruteo = {f: (np.array(PRIORS[f][1 : 1 + N_GRUPOS]), float(PRIORS[f][1 + N_GRUPOS])) for f in BUCKET_IDS}
    mejor_ajuste: dict[str, dict] = {}
    mejor_detalle: dict[str, list] = {}

    for iteracion in range(1, ITERACIONES_RUTA + 1):
        print(f"\n--- Iteración {iteracion}/{ITERACIONES_RUTA} ---")
        for franja in BUCKET_IDS:
            observaciones = mediciones.get(franja, [])
            if not observaciones:
                if franja not in mejor_ajuste:
                    mejor_ajuste[franja] = {
                        "t0_s": float(PRIORS[franja][0]),
                        "g": [round(v, 4) for v in PRIORS[franja][1 : 1 + N_GRUPOS]],
                        "d_cruce_s": float(PRIORS[franja][1 + N_GRUPOS]),
                        "n": 0,
                        "rmse_min": None,
                        "error_max_min": None,
                        "pesos": [],
                    }
                continue

            factores, demora = ruteo[franja]
            filas, detalle = [], []
            for id_par, minutos in observaciones:
                if id_par not in nodos:
                    print(f"  Aviso: par desconocido '{id_par}', se ignora.")
                    continue
                origen, destino = nodos[id_par]
                _t, ff, cruces = dijkstra_hasta(grafo, origen, destino, factores, demora)
                if ff is None:
                    print(f"  Aviso: no hay ruta para '{id_par}', se ignora.")
                    continue
                filas.append((ff, cruces, minutos * 60.0))
                detalle.append((id_par, minutos, ff, cruces))

            if len(filas) < 3:
                print(f"  {franja}: muy pocas mediciones utilizables, se deja el prior.")
                continue

            ajuste = ajustar_franja(filas, PRIORS[franja])

            previo = mejor_ajuste.get(franja)
            if previo is None or previo["rmse_min"] is None or ajuste["rmse_min"] < previo["rmse_min"]:
                mejor_ajuste[franja] = ajuste
                mejor_detalle[franja] = detalle
                marca = "*"
            else:
                marca = " "

            # Amortiguación: se cree solo una parte del ajuste nuevo al volver a
            # rutear. Sin esto la composición de la ruta salta entre autopista y
            # arteria de una vuelta a otra y el ajuste oscila sin converger.
            nuevos_factores, nueva_demora = _parametros_de_ruteo(ajuste)
            ruteo[franja] = (
                AMORTIGUACION * nuevos_factores + (1 - AMORTIGUACION) * factores,
                AMORTIGUACION * nueva_demora + (1 - AMORTIGUACION) * demora,
            )

            factores_txt = "  ".join(f"{n}={v:.2f}" for n, v in zip(GRUPOS, ajuste["g"]))
            print(
                f" {marca}{franja:12s} t0={ajuste['t0_s']:5.0f}s  {factores_txt}  "
                f"cruce={ajuste['d_cruce_s']:5.1f}s   rmse={ajuste['rmse_min']:5.2f} min"
            )

    # ------------------------------------------------------------------
    print("\n--- Mejor ajuste, medición por medición ---")
    diagnostico_pares = []
    sospechosos: dict[str, int] = {}

    for franja in BUCKET_IDS:
        if franja not in mejor_detalle:
            continue
        ajuste = mejor_ajuste[franja]
        t0 = ajuste["t0_s"]
        factores = np.array(ajuste["g"])
        demora = ajuste["d_cruce_s"]
        pesos = ajuste.get("pesos") or [1.0] * len(mejor_detalle[franja])

        for (id_par, observado, ff, cruces), peso in zip(mejor_detalle[franja], pesos):
            predicho = (t0 + float(np.dot(ff, factores)) + cruces * demora) / 60.0
            error = predicho - observado
            diagnostico_pares.append(
                {
                    "franja": franja,
                    "par": id_par,
                    "observado_min": round(observado, 1),
                    "predicho_min": round(predicho, 1),
                    "error_min": round(error, 1),
                    "cruces": int(cruces),
                    "peso_robusto": round(float(peso), 2),
                }
            )
            if peso < 0.6:
                sospechosos[id_par] = sospechosos.get(id_par, 0) + 1
            marca = "!" if abs(error) > 5 else " "
            print(
                f" {marca} {franja:12s} {id_par:22s} obs={observado:5.1f}  pred={predicho:5.1f}  "
                f"err={error:+5.1f} min  ({cruces:3d} cruces, peso {peso:.2f})"
            )

    errores = np.array([d["error_min"] for d in diagnostico_pares])
    rmse_global = float(np.sqrt(np.mean(errores**2))) if errores.size else None
    mae = float(np.mean(np.abs(errores))) if errores.size else None
    grandes = int(np.sum(np.abs(errores) > 5)) if errores.size else 0

    print(
        f"\nRMSE global: {rmse_global:.2f} min | error absoluto medio: {mae:.2f} min | "
        f"{grandes}/{len(errores)} mediciones con error > 5 min."
    )
    if sospechosos:
        print("\nPares que el modelo no logra explicar (reponderados a la baja):")
        for id_par, veces in sorted(sospechosos.items(), key=lambda x: -x[1]):
            print(f"  {id_par}: castigado en {veces} de 5 franjas")
        print("  Revisa la coordenada del par en config.py y la ruta que toma Google.")

    escribir_traffic_json(
        mejor_ajuste,
        fuente=f"{total} mediciones de Google Maps",
        diagnostico={
            "rmse_global_min": round(rmse_global, 2) if rmse_global else None,
            "mae_min": round(mae, 2) if mae else None,
            "mediciones_con_error_alto": grandes,
            "mediciones": diagnostico_pares,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
