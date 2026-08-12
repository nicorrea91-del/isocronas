"""
Genera data/mediciones.csv: la plantilla que se llena a mano con tiempos de
Google Maps para calibrar el tráfico.

Uso:
    python pipeline/make_plantilla.py

No sobrescribe un archivo existente salvo que se pase --force, para no borrar
mediciones ya ingresadas.
"""

import csv
import sys

from config import CALIBRATION_PAIRS, DATA_DIR, TIME_BUCKETS

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


def main() -> int:
    destino = DATA_DIR / "mediciones.csv"
    force = "--force" in sys.argv

    if destino.exists() and not force:
        print(f"Ya existe {destino}. Usa --force para regenerarla (se pierde lo ingresado).")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    filas = []
    for par in CALIBRATION_PAIRS:
        for bucket in TIME_BUCKETS:
            filas.append(
                {
                    "id_par": par["id"],
                    "franja": bucket["id"],
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

    with destino.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows(filas)

    print(f"Escrito {destino} con {len(filas)} filas por llenar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
