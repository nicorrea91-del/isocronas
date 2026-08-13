"""
Genera o actualiza data/mediciones.csv: la plantilla que se llena a mano con
tiempos de Google Maps para calibrar el tráfico.

Es **incremental**. Si el archivo ya existe, conserva todo lo que hayas escrito y
solo agrega las combinaciones de par y franja que falten. Así se pueden sumar
pares nuevos sin perder ni una medición.

Uso:
    python pipeline/make_plantilla.py            # agrega lo que falte
    python pipeline/make_plantilla.py --desde-cero
"""

import csv
import sys

from config import CALIBRATION_PAIRS, DATA_DIR, FIT_BUCKET_IDS, TIME_BUCKETS

COLUMNS = [
    "id_par",
    "franja",
    "dia",
    "hora_salida",
    "origen",
    "destino",
    "minutos",
    "minutos_min",
    "minutos_max",
    "notas",
]

BUCKET_POR_ID = {b["id"]: b for b in TIME_BUCKETS}


def _franjas_del_par(par) -> list[str]:
    """Un par puede pedir solo algunas franjas; por defecto, todas las ajustables."""
    pedidas = par.get("franjas")
    if not pedidas:
        return list(FIT_BUCKET_IDS)
    return [f for f in pedidas if f in FIT_BUCKET_IDS]


def _filas_esperadas() -> list[dict]:
    filas = []
    for par in CALIBRATION_PAIRS:
        for franja in _franjas_del_par(par):
            bucket = BUCKET_POR_ID[franja]
            filas.append(
                {
                    "id_par": par["id"],
                    "franja": franja,
                    "dia": bucket["dia"],
                    "hora_salida": bucket["hora"],
                    "origen": par["origen"],
                    "destino": par["destino"],
                    "minutos": "",
                    "minutos_min": "",
                    "minutos_max": "",
                    "notas": "",
                }
            )
    return filas


def _leer_existentes(ruta) -> dict[tuple[str, str], dict]:
    if not ruta.exists():
        return {}
    with ruta.open(encoding="utf-8-sig", newline="") as fh:
        lector = csv.DictReader(fh, delimiter=";")
        return {
            ((fila.get("id_par") or "").strip(), (fila.get("franja") or "").strip()): fila
            for fila in lector
            if (fila.get("id_par") or "").strip()
        }


def main() -> int:
    destino = DATA_DIR / "mediciones.csv"
    desde_cero = "--desde-cero" in sys.argv

    existentes = {} if desde_cero else _leer_existentes(destino)
    esperadas = _filas_esperadas()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    nuevas = 0
    conservadas = 0
    con_dato = 0
    filas_finales = []

    for fila in esperadas:
        clave = (fila["id_par"], fila["franja"])
        previa = existentes.get(clave)
        if previa:
            # Se mantienen los valores ingresados a mano; el resto se refresca
            # desde config.py, por si cambió una dirección o una hora.
            for columna in ("minutos", "minutos_min", "minutos_max", "notas"):
                fila[columna] = (previa.get(columna) or "").strip()
            conservadas += 1
            if fila["minutos"] or fila["minutos_min"] or fila["minutos_max"]:
                con_dato += 1
        else:
            nuevas += 1
        filas_finales.append(fila)

    huerfanas = set(existentes) - {(f["id_par"], f["franja"]) for f in esperadas}

    with destino.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(filas_finales)

    print(f"{destino}")
    print(f"  {len(filas_finales)} filas en total")
    print(f"  {conservadas} conservadas ({con_dato} con medición ya cargada)")
    print(f"  {nuevas} nuevas por llenar")
    if huerfanas:
        pares = sorted({par for par, _ in huerfanas})
        print(f"  {len(huerfanas)} filas descartadas de pares que ya no existen: {', '.join(pares)}")

    if nuevas:
        print("\nFilas nuevas, agrupadas por par:")
        pendientes: dict[str, list[str]] = {}
        claves_previas = set(existentes)
        for fila in filas_finales:
            if (fila["id_par"], fila["franja"]) in claves_previas:
                continue
            pendientes.setdefault(fila["id_par"], []).append(
                f'{fila["dia"]} {fila["hora_salida"]}'
            )
        for id_par, horarios in pendientes.items():
            par = next(p for p in CALIBRATION_PAIRS if p["id"] == id_par)
            print(f"\n  {id_par}  ({par['por_que']})")
            print(f"    {par['origen']}")
            print(f"    -> {par['destino']}")
            print(f"    horarios: {', '.join(horarios)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
