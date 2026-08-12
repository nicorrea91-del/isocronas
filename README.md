# Isócronas y centro de gravedad — Santiago

Herramienta para decidir **dónde conviene vivir** según los minutos de auto que
gastas realmente cada semana: el trabajo, el colegio de los niños, los suegros,
los papás. Cada punto se pondera por cuántos viajes haces a la semana (0 a 14) y
se le asigna el horario en que efectivamente viajas — punta de la mañana, punta
de la tarde, valle o noche.

El resultado no es un punto: es un **mapa de calor de minutos semanales** sobre
las comunas que te interesan, con el óptimo marcado. Ver la cuenca completa
importa más que el punto exacto, porque muestra cuánto pierdes si te corres tres
cuadras.

---

## Cómo resuelve las isócronas sin quemar APIs

La pregunta obvia es: *¿cómo sabes hasta dónde llego en 20 minutos sin consultar
infinitas direcciones en Google Maps?* No se consulta ninguna.

1. **Se baja una vez la red de calles** de OpenStreetMap para el rectángulo de
   interés. Nodos = esquinas, aristas = tramos con su largo real y una velocidad
   según el tipo de vía.
2. **Un solo Dijkstra** desde el punto entrega el tiempo hasta *todas* las
   esquinas de la ciudad de una pasada. Las que quedan bajo 20 minutos son la
   isócrona, y su borde sigue las calles solo — con los tentáculos sobre las
   autopistas, que es justo lo que importa.
3. **El centro de gravedad sale de la misma máquina, al revés.** Para cada punto
   se corre un Dijkstra sobre el grafo invertido, que da el tiempo *desde
   cualquier lugar hacia* ese punto:

   ```
   costo(candidato) = Σ_i  viajes_i · ( t(candidato → punto_i) + t(punto_i → candidato) ) / 2
   ```

   El mínimo de ese campo es el centro de gravedad real sobre la red — no el
   promedio de coordenadas, que puede caer en la mitad del cerro Manquehue. Las
   curvas de nivel son el mapa de calor.

Con seis puntos son doce búsquedas de unos 100 ms. Mover el slider de viajes
semanales no recalcula nada: solo recombina campos ya guardados, así que la
respuesta es instantánea. **Cero llamadas a API en tiempo de uso.**

### El tráfico

La velocidad de cada tramo se modela como

```
tiempo = tiempo_flujo_libre × g(tipo_de_vía, franja_horaria)  +  t0(franja)
```

donde `g` son factores de demora y `t0` el costo fijo del viaje (partir,
estacionar, los primeros y últimos 100 metros). Esos números se ajustan con unas
40 mediciones reales de Google Maps que se ingresan a mano una sola vez.

Mientras no haya mediciones, el proyecto usa valores a priori estimados con la
forma de un día típico de Santiago — dos puntas, la de la tarde peor que la de
la mañana. **Son un supuesto, no un dato**, y están por lo bajo: sin calibrar,
el modelo da Chicureo–El Golf en 33 minutos en punta de la mañana, cuando en la
práctica son bastante más. El
[índice de tráfico de TomTom](https://www.tomtom.com/traffic-index/city/santiago/)
sirve para contrastar la forma de la curva, pero la magnitud la fijan las
mediciones.

Son 4 incógnitas por franja contra 8 mediciones, así que el ajuste es una
regresión chica y bien determinada. Los 8 pares origen-destino están elegidos
con composiciones de vía muy distintas a propósito: si todos fueran puro
autopista, no habría forma de separar el factor de las arterias del de las
autopistas.

---

## Puesta en marcha

Requiere Python 3.10+ y `numpy`. No necesita Node ni Docker.

```bash
python -m pip install numpy
```

### 1. Bajar los datos de OpenStreetMap

```bash
python pipeline/fetch_osm.py
```

Descarga la red viaria por tiles desde la API pública de Overpass y la deja
cacheada en `cache/`. Toma varios minutos la primera vez y **nunca más**: las
corridas siguientes leen del caché. Si un tile es muy grande para el servidor,
se subdivide solo.

### 2. Construir el grafo

```bash
python pipeline/build_graph.py
```

Genera `web/data/graph.bin`, `web/data/grid.bin` y `web/data/graph_meta.json`.

### 3. Factores de tráfico

```bash
python pipeline/calibrate.py --priors
```

Escribe `web/data/traffic.json` con valores a priori. **La app ya funciona acá**,
con tráfico aproximado. Para calibrarla de verdad, ver la sección siguiente.

### 4. Levantar el sitio

```bash
python -m http.server 8000 --directory web
```

Y abrir <http://localhost:8000>. Tiene que ser por servidor: abrir el
`index.html` con doble clic no funciona, porque el navegador bloquea la lectura
de los archivos de datos.

---

## Calibrar el tráfico con mediciones reales

Este es el único trabajo manual del proyecto, y se hace una vez.

`data/mediciones.csv` viene con 40 filas: 8 pares origen-destino × 5 franjas
horarias. Hay que llenar la columna `minutos` con lo que dice Google Maps.

Para cada fila:

1. Abrir Google Maps y pedir la ruta **en auto** entre `origen` y `destino`.
2. Poner *Salir a las…* con el `dia` y la `hora_salida` de la fila.
3. Anotar el tiempo típico en `minutos`. Si Google da un rango ("25–40 min"),
   puedes anotar los extremos en `minutos_min` y `minutos_max` y dejar `minutos`
   vacío: se usa el punto medio.

Las direcciones son las que están en `origen` y `destino`; las coordenadas
exactas viven en `CALIBRATION_PAIRS` dentro de `pipeline/config.py` por si
quieres corregir alguna.

Después:

```bash
python pipeline/calibrate.py
```

Imprime los factores ajustados, el error de cada medición y el RMSE global, y
reescribe `web/data/traffic.json`. Un RMSE bajo los 3 minutos es bueno. Si una
medición aparece marcada con `!` (error mayor a 5 min), suele ser que la ruta que
tomó Google es distinta a la del modelo — vale la pena mirarla.

Las filas que dejes vacías simplemente no se usan; el proyecto funciona con las
que haya, y las franjas sin mediciones se quedan con su valor a priori.

---

## Estructura

```
pipeline/
  config.py          Área, comunas, velocidades, franjas, pares de calibración
  fetch_osm.py       Descarga de Overpass con caché y subdivisión de tiles
  build_graph.py     Contracción, densificación y export binario
  geometry.py        Distancias, polígonos comunales, índice espacial
  calibrate.py       Ajuste de los factores de tráfico
  graph_io.py        Lectura del grafo desde Python
  make_plantilla.py  Regenera data/mediciones.csv
data/
  mediciones.csv     Las 40 mediciones de Google Maps (se llena a mano)
web/
  index.html
  style.css
  js/graph.js        Carga de binarios, grafo invertido, nodo más cercano
  js/routing.js      Dijkstra, campo de costo semanal, rasterizado
  js/render.js       Bandas de isócrona y mapa de calor en canvas
  js/app.js          Mapa, panel, estado y enlace para compartir
  data/              Generado por el pipeline
cache/               Respuestas de Overpass (no se versiona)
```

## Área cubierta

- **Grafo de ruteo:** lat −33.52 a −33.15, lon −70.82 a −70.44. Es más grande
  que la zona de interés a propósito: Chicureo–Las Condes se hace por Los
  Libertadores, Vespucio Norte y Costanera Norte, así que hay que incluir Colina
  rural, Quilicura, Huechuraba y Recoleta o el ruteo inventa desvíos.
- **Zona de análisis** (donde se busca el óptimo): Lo Barnechea, Las Condes,
  Vitacura, Providencia, La Reina, Ñuñoa y los sectores urbanos de Chicureo y
  Chamisero.

Para cambiarla, editar `GRAPH_BBOX`, `ANALYSIS_COMUNAS` y
`ANALYSIS_EXTRA_BOXES` en `pipeline/config.py` y volver a correr el pipeline.

## Publicar en GitHub Pages

```bash
git add -A
git commit -m "Proyecto de isócronas y centro de gravedad"
git branch -M main
git remote add origin git@github.com:USUARIO/REPO.git
git push -u origin main
```

En el repo: *Settings → Pages → Source: Deploy from a branch → main → /web*.
Queda publicado en `https://USUARIO.github.io/REPO/`.

Los archivos de `web/data/` **sí** se versionan (unos pocos MB), para que el
sitio publicado funcione sin correr el pipeline.

## Cómo usarlo

- **Clic en el mapa** agrega un punto. **Shift + clic** evalúa un lugar concreto
  y lo compara contra el óptimo.
- El slider de cada punto son **viajes sueltos por semana**: ida y vuelta cuentan
  2, así que 14 es un viaje redondo todos los días.
- Las franjas de **ida** y **vuelta** se eligen por separado: al trabajo se va en
  punta de la mañana y se vuelve en punta de la tarde; a los suegros se va en
  valle y se vuelve de noche.
- Los tres sliders de isócrona van de 0 a 100; en 0 se apagan.
- *Copiar enlace para compartir* guarda todo el escenario en la URL.

## Límites que vale la pena tener claros

- **El tráfico es un promedio por tipo de vía, no un estado real.** El modelo no
  sabe que hoy hay un choque en Kennedy. Sirve para comparar lugares, no para
  planificar un viaje.
- **Los factores son de toda la ciudad.** Un mismo factor de "arterial" aplica a
  Irarrázaval y a Camino Chicureo. Con más mediciones se podría separar por
  corredor; con 40 no alcanza y sobreajustaría.
- **Solo auto.** Nada de transporte público, bici ni caminata.
- **El óptimo es un punto de la grilla de 150 m**, y el mapa de calor suele ser
  bastante plano cerca del mínimo. Tomar la zona verde como respuesta, no la
  coordenada exacta.
- **Las celdas a más de 300 m de una calle habitable se descartan**, así que
  cerros, el Mapocho y las canchas de golf salen del mapa solos.
