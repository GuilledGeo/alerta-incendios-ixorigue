# alerta-incendios-ixorigue

Panel Streamlit de riesgo de incendio para las ganaderías de clientes de Ixorigue (España): cruza
automáticamente el perímetro real (o aproximado) de cada finca contra los hotspots de incendio de
Google Earth Engine (VIIRS NOAA-20/SNPP + FIRMS) y genera un ranking de ganaderías en riesgo, con
distancia, dirección, tendencia de avance del fuego y datos de contacto del cliente.

**Esta es la versión de despliegue público** de la app — vive originalmente en el monorepo interno
`ixo-geospacial` (`side_projects/alerta_incendios`). Aquí no hay ninguna credencial de base de
datos ni acceso a la BD de producción: la lista de ranchos/clientes se lee de un **snapshot local**
(`data/ranchos_snapshot.gpkg`, no incluido en el repo — ver más abajo), mientras que los **datos de
incendio siguen siendo en tiempo real** vía Google Earth Engine.

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

## Cómo ejecutar en local

```
pip install -r requirements.txt
streamlit run app.py
```

Necesitas dos cosas antes de arrancar:

1. **El snapshot de ranchos** en `data/ranchos_snapshot.gpkg`. Este repo no tiene acceso a la BD
   de producción a propósito — el snapshot se genera desde el monorepo interno `ixo-geospacial`
   (`side_projects/alerta_incendios/scripts/exportar_snapshot.py`, que sí tiene las credenciales)
   y se copia manualmente aquí. Refréscalo con la periodicidad que necesites (semanal, por
   ejemplo) repitiendo ese proceso — la cabecera de la app muestra la fecha del snapshot en uso.

2. **Credenciales de Google Earth Engine**: copia `.streamlit/secrets.toml.example` a
   `.streamlit/secrets.toml` y rellena `GEE_SERVICE_ACCOUNT_EMAIL` / `GEE_SERVICE_ACCOUNT_KEY_JSON`
   con una cuenta de servicio (rol "Earth Engine Resource Viewer"), y `RANCHOS_DATA_SOURCE = "snapshot"`.

## Despliegue en Streamlit Community Cloud

1. Conecta este repo en [share.streamlit.io](https://share.streamlit.io), apuntando a `app.py`.
2. Pega el contenido de `.streamlit/secrets.toml.example` (relleno) en el panel "Secrets" del
   despliegue.
3. Sube `data/ranchos_snapshot.gpkg` al entorno desplegado (no va en git) — o genera el snapshot
   directamente ahí si en algún momento tiene acceso puntual a la BD.

## Qué NO es esta app

No sustituye a ningún sistema de emergencias oficial. Los satélites VIIRS/FIRMS pasan solo unas
pocas veces al día (ver el panel "ℹ️ Cómo funciona" dentro de la app) — es una alerta temprana
orientativa, no monitorización continua.
