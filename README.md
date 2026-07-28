# alerta-incendios-ixorigue

> ## ⚠️ Este es el repo que corre en producción — los fixes de código se editan en `ixo-geospacial`
>
> El desarrollo (con acceso a BD real) se hace en el monorepo interno
> `E:\1_IXORIGUE\1_Proyectos\ixo-geospacial\side_projects\alerta_incendios\`. Este repo es una
> copia de despliegue de ese código (`app.py`, `src/*.py`), **sin sincronización automática entre
> ambos**. Cualquier bugfix o cambio hecho solo en el monorepo interno NO llega aquí solo, y por
> tanto no se ve en la app real hasta que se copia y commitea aparte en este repo — ya pasó el
> 28-jul-2026. Antes de dar un cambio por terminado, verificar con `diff` que los archivos tocados
> quedan idénticos en ambos repos (`ranches.py` es la excepción esperada: aquí solo existe el modo
> `snapshot`, no `db`). Ver la sección "DOS REPOS SEPARADOS" en el `README.md` del monorepo interno
> para más detalle.

Panel Streamlit de riesgo de incendio para las ganaderías de clientes de Ixorigue (España): cruza
automáticamente el perímetro real (o aproximado) de cada finca contra los hotspots de incendio de
Google Earth Engine (VIIRS NOAA-20/SNPP + FIRMS) y genera un ranking de ganaderías en riesgo, con
distancia, dirección, tendencia de avance del fuego y datos de contacto del cliente.

**Esta es la versión de despliegue público** de la app — vive originalmente en el monorepo interno
`ixo-geospacial` (`side_projects/alerta_incendios`). Aquí no hay ninguna credencial de base de
datos ni acceso a la BD de producción: la lista de ranchos/clientes se lee de un **snapshot**
(generado aparte y pasado como secret, ver más abajo — nunca vive en el repo), mientras que los
**datos de incendio siguen siendo en tiempo real** vía Google Earth Engine.

## Estructura

```
app.py                        entry point — streamlit run app.py
src/
├── config.py                   parámetros, colores, umbrales, origen de datos (RANCHOS_DATA_SOURCE)
├── ranches.py                  carga de ranchos: BD (interno) o snapshot local (este repo)
├── gee_hotspots.py              cliente Earth Engine (VIIRS + FIRMS)
├── risk.py                      distancia, dirección, anillo de riesgo, mensaje de aviso
├── fires.py                     clustering de hotspots en incendios, geocoding, avance del fuego
└── geo_utils.py                 bearing/haversine compartidos
.streamlit/secrets.toml.example  plantilla de secrets (copiar a secrets.toml, nunca commitear el real)
data/                            snapshot local de ranchos (.gpkg, gitignored)
```

## Generar/refrescar el snapshot de ranchos

Este repo no tiene acceso a la BD de producción a propósito. El snapshot se genera desde el
monorepo interno `ixo-geospacial` (que sí tiene las credenciales) y viaja como secret en base64,
nunca como archivo en el repo:

```
# desde ixo-geospacial/side_projects/alerta_incendios, con tu .env de BD configurado
python scripts/exportar_snapshot.py

# codificar el resultado en base64 (Windows)
certutil -encode data/ranchos_snapshot.gpkg snapshot_b64.txt
# (Linux/Mac: base64 -w0 data/ranchos_snapshot.gpkg > snapshot_b64.txt)
```

Pega el contenido de `snapshot_b64.txt` como `RANCHOS_SNAPSHOT_B64` en los Secrets (ver
`.streamlit/secrets.toml.example`). Repite esto con la periodicidad que necesites (p. ej.
semanal) — la cabecera de la app muestra la fecha de exportación del snapshot en uso.

## Persistencia del ranking de riesgo (`RANK_HISTORY_SHEET_ID`, opcional)

Streamlit Community Cloud no tiene disco persistente: sin este secret, las flechas de
subida/bajada del ranking (`src/rank_history.py`) se resetean (todo "🆕 Nuevo") cada vez que el
contenedor se reinicia (duerme por inactividad o se redespliega). Con él, el historial se guarda
en una Google Sheet en vez de en un archivo local. Reutiliza la misma cuenta de servicio de
`GEE_SERVICE_ACCOUNT_EMAIL`/`KEY_JSON` (no hace falta crear una nueva) — ver los 4 pasos
detallados en `.streamlit/secrets.toml.example`.

## Cómo ejecutar en local

```
pip install -r requirements.txt
streamlit run app.py
```

Copia `.streamlit/secrets.toml.example` a `.streamlit/secrets.toml` y rellena
`RANCHOS_SNAPSHOT_B64` (ver arriba) y `GEE_SERVICE_ACCOUNT_EMAIL` / `GEE_SERVICE_ACCOUNT_KEY_JSON`
(cuenta de servicio de Google Earth Engine, rol "Earth Engine Resource Viewer" — necesaria porque
un servidor desplegado no puede hacer `earthengine authenticate` de forma interactiva).

## Despliegue en Streamlit Community Cloud

1. Conecta este repo en [share.streamlit.io](https://share.streamlit.io), apuntando a `app.py`.
2. Pega el contenido de tu `secrets.toml` (relleno) en el panel "Secrets" del despliegue.

## Qué NO es esta app

No sustituye a ningún sistema de emergencias oficial. Los satélites VIIRS/FIRMS pasan solo unas
pocas veces al día (ver el panel "ℹ️ Cómo funciona" dentro de la app) — es una alerta temprana
orientativa, no monitorización continua.
