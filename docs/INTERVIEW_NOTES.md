# Notas para entrevistas — fuel-prices-pipeline

Respuestas cortas a las preguntas más probables. Si te preguntan algo que no está aquí, la
respuesta suele estar en un comentario del código o en el commit que lo introdujo.

## Visión general

**¿Qué hace el proyecto en una frase?**
Descarga cada día los precios de las ~11.500 gasolineras de España desde la API pública del
Ministerio, guarda el JSON crudo, lo limpia con pandas y lo carga en un modelo en estrella en
PostgreSQL, donde lo analizo con SQL.

**¿Por qué este dataset?**
Es real, público, sin API key, cambia a diario (permite análisis temporal) y tiene problemas
típicos de datos reales: decimales con coma, todo como texto, nulos como cadena vacía,
espacios sobrantes y columnas con tildes.

**¿Es ETL o ELT?**
ETL: transformo en Python antes de cargar. Pero guardo el crudo (capa *raw*), así que puedo
reprocesar cualquier día sin volver a llamar a la API, que es la ventaja principal de ELT.

## Extracción

**¿Qué pasa si la API falla?**
Reintento con *backoff* exponencial (2 s, 4 s…) solo en errores transitorios: timeouts,
conexión, 429 y 5xx. Un 404 o 400 no se reintenta porque no se va a arreglar solo. Además valido
el sobre (`ResultadoConsulta == "OK"` y lista no vacía) **antes** de guardar el crudo, para no
persistir respuestas basura. En un backfill, un día que falla no para los demás; al final el
proceso devuelve código 1 y lista los días fallidos, para que el programador de tareas avise.

**¿Por qué guardar el crudo comprimido?**
Cada día son ~12 MB de JSON; en gzip ~0,7 MB. Es la "fuente de verdad": si mañana descubro un
bug en la transformación, reproceso sin depender de la API. El fichero se escribe en un `.tmp` y
se renombra (escritura atómica): nunca queda un fichero a medias.

**¿Cómo cargaste el histórico?**
La API tiene un endpoint `EstacionesTerrestresHist/{dd-mm-aaaa}`. Lo comprobé antes de diseñar.
Los días históricos se cachean (no cambian); el día actual se descarga siempre porque los
precios se actualizan cada 30 minutos.

**¿Qué fecha asignas a un snapshot?**
La del campo `Fecha` de la propia API, no la del reloj de mi máquina. Si pido un histórico y la
API devuelve otro día, lo trato como error.

## Transformación

**¿Qué limpieza haces?**
- Decimales: `"1,799"` → `1.799` (sustituyo la coma y uso `to_numeric(errors="coerce")`).
- Nulos: `""` → `NaN`; esas filas desaparecen al pasar a formato largo.
- Texto: `strip` y colapso de espacios; vacíos → NA.
- Códigos con ceros a la izquierda (`"02250"`, `"01"`) se quedan como texto: son
  identificadores, no números.
- Precios fuera de (0, 50) €/l se descartan con un *warning* en el log.
- Estaciones duplicadas: me quedo con la última y lo registro.

**¿Por qué formato largo (una fila por estación/combustible/día) y no 23 columnas?**
Casi todas las estaciones venden 3–5 de los 23 combustibles: en ancho tendría la mayoría de
celdas vacías. En largo no guardo nulos, añadir un combustible nuevo no cambia el esquema y las
consultas "por combustible" son un simple `WHERE`/`GROUP BY`.

**¿Cómo generas los códigos de combustible?**
De la cabecera de columna: quito "Precio ", elimino tildes con `unicodedata` y paso a
`snake_case` (`Diésel Renovable` → `diesel_renovable`). Si el Ministerio añade un combustible,
entra solo.

**¿Qué testeas?**
Unitarios (pytest) de la transformación: decimales, nulos, tipos, ceros a la izquierda, columnas
obligatorias, duplicados, rangos y el flujo completo con un *fixture* de 5 estaciones reales.
Integración contra un PostgreSQL real (en CI con un *service container*): cargar dos veces da el
mismo resultado, un re-run elimina precios que desaparecen y un día antiguo no sobrescribe una
estación más reciente.

## Modelo de datos

**¿Por qué un esquema en estrella?**
Es el estándar para analítica (Kimball): una tabla de hechos estrecha con métricas y claves, y
dimensiones descriptivas. Las consultas son joins simples y predecibles.

**¿Cuál es el *grain* de la tabla de hechos?**
Un precio por estación, combustible y día. La clave primaria `(date_key, station_id, fuel_id)`
lo garantiza a nivel de base de datos.

**¿Por qué provincia y municipio están dentro de `dim_station`?**
Desnormalizar evita joins extra (snowflake) y esos datos casi no cambian. Para 11.500 filas la
redundancia es despreciable.

**¿Para qué sirve `dim_date` si ya tienes la fecha?**
Precalcula atributos (día de la semana, fin de semana, semana ISO, trimestre) para no repetir
`EXTRACT` en cada consulta, y es el patrón estándar. La uso en el análisis "¿es más barato algún
día de la semana?". Uso `date_key` entero `AAAAMMDD`: legible y ordenable.

**¿Qué tipo de SCD es `dim_station`?**
Tipo 1: sobrescribo con la versión más reciente. Guardo `first_seen` y `last_seen`. Si cargo un
día antiguo después de uno nuevo (backfill desordenado), no piso atributos: el upsert tiene
`WHERE EXCLUDED.last_seen >= d.last_seen`. Si necesitara historial de cambios de marca o
dirección, pasaría a tipo 2 (filas con `valid_from`/`valid_to`).

**¿Por qué `numeric(6,3)` y no `float`?**
Los precios tienen exactamente 3 decimales y `numeric` es exacto: no aparecen cosas como
`1.7989999`.

## Carga e idempotencia

**¿Cómo garantizas que ejecutar dos veces no duplica?**
Clave primaria natural más `INSERT … ON CONFLICT DO UPDATE` (upsert). Además, todo un día se
carga en **una transacción**: o entra completo o no entra nada.

**¿Y si entre dos ejecuciones del mismo día una estación deja de vender un combustible?**
El upsert no borraría esa fila vieja. Como `now()` es constante dentro de una transacción, todas
las filas escritas tienen `loaded_at = now()`; después borro las de ese día con
`loaded_at < now()`. El día queda idéntico al último snapshot.

**¿Por qué COPY a tablas temporales?**
`COPY` es la forma más rápida de meter datos en PostgreSQL (mucho más que `INSERT` fila a fila).
Cargo en tablas `TEMP … ON COMMIT DROP` y desde ahí hago el upsert con SQL.

**¿Algún problema de rendimiento?**
Sí, y es buena historia. La primera versión borraba los obsoletos con `NOT EXISTS` contra la
tabla temporal: ~80 s por día. Mirando `pg_stat_activity` vi que era esa consulta. La tabla
temporal no tenía estadísticas y el planificador eligió un bucle anidado (43k × 43k). Lo
reescribí con el truco de `loaded_at < now()`, que usa el índice de la PK: ~3 s.

**Otro problema que encontraste**
Los tests de integración tardaban 10 s en conectar. En Windows `localhost` resuelve primero a
IPv6 (`::1`), pero Docker solo publicaba el puerto en `127.0.0.1`, así que esperaba al timeout.
Solución: usar `127.0.0.1` explícito.

## SQL de análisis

**¿Diferencia entre RANK, DENSE_RANK y ROW_NUMBER?**
Con empates: `ROW_NUMBER` da 1,2,3; `RANK` da 1,1,3; `DENSE_RANK` da 1,1,2. Uso `DENSE_RANK`
para "las más baratas" porque muchas gasolineras comparten precio.

**¿Por qué `RANGE BETWEEN INTERVAL '6 days' PRECEDING` y no `ROWS BETWEEN 6 PRECEDING`?**
`ROWS` cuenta filas: si falta un día, la ventana se estira hacia atrás sin avisar. `RANGE` con
intervalo sobre la fecha significa "los últimos 7 días naturales", falten o no.

**¿Para qué los CTE?**
Legibilidad: cada paso tiene nombre (`latest`, `province_avg`…) y se lee de arriba abajo. En
PostgreSQL 12+ el planificador puede integrarlos en la consulta, así que no penalizan.

## Ingeniería

**¿Cómo gestionas configuración y secretos?**
Variables de entorno leídas en `config.py`, con un `.env` local que está en `.gitignore`. En el
repo solo hay `.env.example`. El Postgres de Docker solo escucha en `127.0.0.1`.

**¿Qué hace el CI?**
En cada push: `ruff check`, `ruff format --check` y pytest, incluida la integración contra un
PostgreSQL levantado como *service container* de GitHub Actions.

**¿Cómo lo programas a diario?**
`scripts/run_daily.cmd` con el Programador de tareas de Windows. Un workflow programado de
GitHub Actions no sirve tal cual: no ve mi base de datos local. Funcionaría si la base estuviera
en la nube o si el workflow guardara el crudo como artefacto o en un bucket.

**¿Por qué no Airflow/Spark?**
11.500 filas por día caben de sobra en pandas y PostgreSQL. Spark añade complejidad sin
beneficio a este volumen. Airflow tiene sentido con varias fuentes, dependencias entre tareas y
reintentos o alertas centralizados: es el siguiente paso natural.

**¿Qué harías a continuación?**
Orquestar con Airflow (o Dagster), modelar las transformaciones SQL con dbt y sus tests,
SCD tipo 2 en estaciones, validaciones de calidad (por ejemplo, alertar si llegan un 20 % menos
de estaciones), crudo en S3/GCS y base gestionada en la nube, y un dashboard
(Metabase/Superset).

**¿Qué parte te costó más?** *(personaliza esta respuesta)*
Sugerencia: el rendimiento del borrado de filas obsoletas, porque obliga a entender planes de
ejecución y transacciones, no solo a escribir SQL.
