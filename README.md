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

El tiempo de una ruta se modela como

```
T = t0(franja)  +  Σ_c ff_c · g(c, franja)  +  N_cruces · d(franja)
```

- `ff_c` son los minutos de flujo libre que la ruta pasa en vías del grupo `c`
  (autopista / arterial / local), que salen del grafo.
- `g` son factores de demora por grupo y franja.
- `t0` es el costo fijo del viaje: partir, estacionar, los primeros y últimos
  100 metros.
- `N_cruces` es la cantidad de intersecciones que atraviesa la ruta, y `d` la
  demora media por intersección.

**El término de intersecciones es lo que hace que el modelo funcione.** Un
modelo puramente multiplicativo no puede describir a la vez un viaje de 4 km por
Pedro de Valdivia y uno de 22 km por Costanera Norte: el primero exigía un
factor arterial de 6 y el segundo de 3. La causa física de la diferencia no es
el tipo de vía sino la densidad de semáforos, y contarlos la captura sin romper
la linealidad del ajuste. Los cruces alcanzados por autopista no cuentan: son
enlaces a distinto nivel, no semáforos.

Todos los parámetros están **acotados a rangos físicos** (`COTAS` en
`calibrate.py`). No es un detalle decorativo: sin cota superior en `t0`, el
ajuste cambia toda la estructura multiplicativa por una constante enorme —llegó
a 20 minutos de costo fijo por viaje— y clava los factores en su piso. Eso da un
RMSE bajísimo en las mediciones y un modelo inservible, porque un candidato a
500 m del destino quedaría predicho en 20 minutos.

Mientras no haya mediciones, el proyecto usa valores a priori estimados con la
forma de un día típico de Santiago. **Son un supuesto, no un dato**, y están por
lo bajo. El
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

Genera `docs/data/graph.bin`, `docs/data/grid.bin` y `docs/data/graph_meta.json`.

### 3. Factores de tráfico

```bash
python pipeline/calibrate.py --priors
```

Escribe `docs/data/traffic.json` con valores a priori. **La app ya funciona acá**,
con tráfico aproximado. Para calibrarla de verdad, ver la sección siguiente.

### 4. Levantar el sitio

```bash
python -m http.server 8000 --directory docs
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
reescribe `docs/data/traffic.json`. Las mediciones marcadas con `!` tienen error
mayor a 5 minutos, y las que el modelo no logra explicar se reponderan a la baja
estilo Huber y se listan al final: así un par con una coordenada mala no arrastra
el ajuste completo.

Las filas que dejes vacías simplemente no se usan; el proyecto funciona con las
que haya, y las franjas sin mediciones se quedan con su valor a priori.

**Estado actual del ajuste** con las 40 mediciones cargadas:

| franja | costo fijo | autopista | arterial | local | demora/cruce | RMSE |
|---|---|---|---|---|---|---|
| madrugada | 4,0 min | 0,87 | 0,70 | 0,70 | 8,3 s | 1,78 min |
| punta AM | 1,5 min | 0,70 | 2,76 | 0,70 | 31,7 s | 4,89 min |
| valle | 4,0 min | 0,82 | 0,92 | 0,70 | 21,3 s | 4,76 min |
| punta PM | 4,0 min | 0,80 | 0,70 | 1,45 | 27,1 s | 6,03 min |
| noche | 4,0 min | 0,70 | 1,80 | 0,70 | 8,5 s | 3,74 min |

RMSE global 4,47 min, error absoluto medio 3,18 min, 8 de 40 mediciones con
error mayor a 5 minutos. La demora por semáforo yendo de 8 s en la madrugada a
32 s en la punta de la mañana es un resultado sano y creíble.

Los dos pares que el modelo todavía no explica bien están documentados en la
sección de límites.

---

## Estructura

```
pipeline/
  config.py          Área, comunas, velocidades, franjas, pares de calibración
  fetch_osm.py       Descarga de Overpass con caché y subdivisión de tiles
  build_graph.py     Contracción, densificación, intersecciones, export binario
  geometry.py        Distancias, polígonos comunales, índice espacial
  calibrate.py       Ajuste de los factores de tráfico
  graph_io.py        Lectura del grafo desde Python
  make_plantilla.py  Crea o actualiza data/mediciones.csv sin perder datos
data/
  mediciones.csv     Mediciones de Google Maps (se llena a mano)
docs/                La app. Se llama "docs" porque es la única subcarpeta
                     desde la que GitHub Pages sabe publicar.
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

El objetivo es una dirección web pública que se le pueda mandar a cualquiera. Es
gratis y no hay que mantener ningún servidor.

### 1. Crear la cuenta y el repositorio

1. Crear cuenta en <https://github.com> si no hay una.
2. Ir a <https://github.com/new>.
3. **Repository name:** `isocronas` (o cualquier nombre sin espacios).
4. Dejarlo en **Public**. Con Public, Pages es gratis; en Private hay que pagar.
5. **No** marcar "Add a README file" ni ninguna de las casillas de abajo: el
   repositorio local ya tiene contenido y hay que subirlo vacío de conflictos.
6. Botón **Create repository**.

### 2. Subir el proyecto

GitHub muestra la dirección del repo recién creado. Con ella, desde la carpeta
del proyecto:

```bash
git remote add origin https://github.com/USUARIO/isocronas.git
git push -u origin main
```

Cambiando `USUARIO` por el nombre de usuario de GitHub. La primera vez va a pedir
autenticación: se abre una ventana del navegador y basta autorizar.

Si Git pide usuario y contraseña en la terminal en vez de abrir el navegador, la
contraseña de la cuenta **no** funciona. Hay que generar un token en
<https://github.com/settings/tokens> (botón *Generate new token (classic)*, marcar
el permiso `repo`) y usar ese token como contraseña.

### 3. Encender Pages

1. En el repo, pestaña **Settings**.
2. Menú lateral izquierdo, **Pages**.
3. En *Build and deployment* → *Source*, elegir **Deploy from a branch**.
4. En *Branch*, elegir `main` y en la carpeta elegir **`/docs`**. Botón **Save**.
5. Esperar uno o dos minutos y recargar. Arriba aparece la dirección:
   `https://USUARIO.github.io/isocronas/`

Esa es la dirección para compartir. Cada vez que se quiera actualizar:

```bash
git add -A
git commit -m "lo que cambió"
git push
```

y en un par de minutos el sitio publicado se actualiza solo.

### Por qué `docs` y no `web`

GitHub Pages solo publica desde la raíz del repositorio o desde una subcarpeta
llamada exactamente `docs`. No acepta otros nombres. Por eso la app vive en
`docs/` aunque no sea documentación.

### Qué queda público

Todo el repositorio: el código, el README y los datos de `docs/data/`. Vale la
pena tener presente dos cosas:

- Los commits de Git llevan el nombre y el correo configurados en
  `git config user.email`.
- `data/mediciones.csv` queda público. No tiene nada sensible, son tiempos de
  viaje entre puntos conocidos.
- Los lugares que se agreguen **en la app** no se publican: viven en el enlace
  que genera el botón *Copiar enlace para compartir*. Ese enlace sí lleva las
  direcciones adentro, así que conviene pensarlo antes de mandarle a alguien un
  enlace con la casa de los suegros marcada.

Los archivos de `docs/data/` se versionan a propósito (unos pocos MB), para que
el sitio publicado funcione sin que nadie tenga que correr el pipeline.

## Cómo usarlo

La app arranca vacía a propósito: los lugares son de quien la usa. Pueden ser la
casa de los suegros y el colegio, o seis clientes que hay que visitar, o las
sucursales de un negocio — es el mismo problema.

Tres formas de agregar lugares:

- **Buscar una dirección** en el buscador (usa Nominatim, gratis).
- **Clic en el mapa**.
- **Agregar varios de una vez**: pegar una lista, una por línea, con el formato
  `Nombre; dirección; viajes`. Por ejemplo:

  ```
  Cliente A; Apoquindo 3000, Las Condes; 3
  Cliente B; Avenida Vitacura 2900, Vitacura; 1
  Colegio; Avenida La Dehesa 1500, Lo Barnechea; 10
  ```

  El nombre y los viajes son opcionales; una línea puede ser solo la dirección.
  Se procesan de a una con una pausa de un segundo, porque el servicio de
  búsqueda es gratuito y pide no abusar.

Y para evaluar:

- **Shift + clic** en el mapa evalúa un lugar concreto y lo compara contra el
  óptimo.
- El slider de cada punto son **viajes sueltos por semana**: ida y vuelta cuentan
  2, así que 14 es un viaje redondo todos los días.
- Las franjas de **ida** y **vuelta** se eligen por separado: al trabajo se va en
  punta de la mañana y se vuelve en punta de la tarde; a los suegros se va en
  valle y se vuelve de noche. Sábado y domingo están disponibles y se tratan como
  ciudad vacía, igual que la madrugada.
- Los tres sliders de isócrona van de 0 a 100; en 0 se apagan.
- *Copiar enlace para compartir* guarda todo el escenario en la URL.

## Límites que vale la pena tener claros

- **El tráfico es un promedio por tipo de vía, no un estado real.** El modelo no
  sabe que hoy hay un choque en Kennedy. Sirve para comparar lugares, no para
  planificar un viaje.
- **La congestión de autopista no es direccional, y ahí está el error que queda.**
  Un solo factor de autopista por franja no puede expresar que Costanera Norte
  hacia el oriente a las 18:30 está tapada mientras la misma autopista al
  poniente fluye. Se ve en los residuos:
  - `ladehesa_losleones` queda **subestimado en 7 a 11 min** en cuatro de las
    cinco franjas. Es un viaje con 62% de autopista, y el modelo no tiene forma
    de cargarle la congestión de ese corredor específico sin romper los otros
    pares que usan la misma autopista.
  - `elgolf_chicureo` en punta de la tarde queda subestimado en 10 min, por lo
    mismo pero al revés: es la salida de la ciudad a la hora peak.

  La solución es separar el grupo `autopista` por corredor y sentido (Costanera
  Norte, Vespucio, Los Libertadores, Kennedy), lo que agrega una incógnita por
  corredor y franja. Con 8 mediciones por franja no alcanza: hacen falta pares
  nuevos que aíslen cada corredor.
- **Los factores de superficie también son de toda la ciudad.** Un mismo factor
  "arterial" aplica a Irarrázaval y a Camino Chicureo.
- **Solo auto.** Nada de transporte público, bici ni caminata.
- **El óptimo es un punto de la grilla de 150 m**, y el mapa de calor suele ser
  bastante plano cerca del mínimo. Tomar la zona verde como respuesta, no la
  coordenada exacta.
- **Las celdas a más de 300 m de una calle habitable se descartan**, así que
  cerros, el Mapocho y las canchas de golf salen del mapa solos.
