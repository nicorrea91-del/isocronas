"""
Lectura del grafo exportado, para poder rutear desde Python (calibración,
pruebas) usando exactamente los mismos datos que el navegador.
"""

import json

import numpy as np

from config import WEB_DATA_DIR

DTYPES = {
    "int32": np.int32,
    "uint16": np.uint16,
    "uint8": np.uint8,
}


class Grafo:
    def __init__(self, meta: dict, buffer: bytes):
        secciones = meta["grafo"]["secciones"]

        def leer(nombre: str) -> np.ndarray:
            seccion = secciones[nombre]
            dtype = DTYPES[seccion["dtype"]]
            inicio = seccion["offset"]
            return np.frombuffer(buffer, dtype=dtype, count=seccion["bytes"] // dtype().itemsize, offset=inicio)

        self.lat = leer("lat").astype(np.float64) / 1e7
        self.lon = leer("lon").astype(np.float64) / 1e7
        self.offsets = leer("offsets")
        self.targets = leer("targets")
        self.base_ds = leer("base_ds")
        self.largo_m = leer("largo_m")
        self.grupos = leer("grupos")
        self.juncion = leer("juncion")
        self.grupos_via = meta["grupos_via"]
        self.meta = meta

    @property
    def n_nodos(self) -> int:
        return len(self.lat)

    @property
    def n_aristas(self) -> int:
        return len(self.targets)

    def nodo_mas_cercano(self, lat: float, lon: float) -> int:
        """Fuerza bruta vectorizada: con ~200k nodos es cuestión de milisegundos."""
        cos_lat = np.cos(np.radians(lat))
        dlat = self.lat - lat
        dlon = (self.lon - lon) * cos_lat
        return int(np.argmin(dlat * dlat + dlon * dlon))


def cargar_grafo() -> Grafo:
    meta = json.loads((WEB_DATA_DIR / "graph_meta.json").read_text(encoding="utf-8"))
    buffer = (WEB_DATA_DIR / meta["grafo"]["archivo"]).read_bytes()
    return Grafo(meta, buffer)
