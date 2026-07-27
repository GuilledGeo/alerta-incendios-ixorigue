import os
from concurrent.futures import ThreadPoolExecutor

import ee
import pandas as pd

from .config import GEE_PROJECT, WINDOW_HOURS_DEFAULT, SCALE_VIIRS, SCALE_MODIS

_initialized = False

# mismas colecciones que el script GEE original (VIIRS20_ID, VIIRSNPP_ID, FIRMS_ID).
# MOD14A1 se dejo fuera: en pruebas (10-jul-2026) devolvia 0 imagenes para la ventana
# tipica de deteccion (72h) - ver PRD.md, "Decisiones de diseño".
SOURCES = {
    "VIIRS_NRT_NOAA20": ("NASA/LANCE/NOAA20_VIIRS/C2", SCALE_VIIRS),
    "VIIRS_NRT_SNPP":   ("NASA/LANCE/SNPP_VIIRS/C2",   SCALE_VIIRS),
    "FIRMS_MODIS_NRT":  ("FIRMS",                       SCALE_MODIS),
}


def _init():
    global _initialized
    if _initialized:
        return
    # despliegue (sin sesion interactiva para `earthengine authenticate`): cuenta de servicio
    # via GEE_SERVICE_ACCOUNT_EMAIL + GEE_SERVICE_ACCOUNT_KEY_JSON (contenido del JSON de la
    # cuenta de servicio, como string). Uso local: credenciales personales ya autenticadas en
    # ~/.config/earthengine/credentials, sin necesidad de estas variables.
    sa_email = os.getenv("GEE_SERVICE_ACCOUNT_EMAIL")
    sa_key_json = os.getenv("GEE_SERVICE_ACCOUNT_KEY_JSON")
    if sa_email and sa_key_json:
        credentials = ee.ServiceAccountCredentials(sa_email, key_data=sa_key_json)
        ee.Initialize(credentials, project=GEE_PROJECT)
    else:
        ee.Initialize(project=GEE_PROJECT)
    _initialized = True


def _puntos_de_coleccion(collection_id: str, scale: int, region: "ee.Geometry",
                          start: "ee.Date", end: "ee.Date") -> "ee.FeatureCollection":
    """Puerto directo de icToPointsRegion() del script GEE original: mascara generica
    (cualquier banda > 0), sample sobre la region, y timestamp con fallback a
    system:time_start si la imagen no trae acq_epoch (caso de FIRMS)."""
    ic = ee.ImageCollection(collection_id).filterDate(start, end).filterBounds(region)

    def _por_imagen(img):
        img = ee.Image(img)
        mask_img = img.toFloat().reduce(ee.Reducer.max()).gt(0)
        sampled = img.updateMask(mask_img).sample(
            region=region, scale=scale, geometries=True, dropNulls=True
        )

        def _por_feature(f):
            acq_epoch = f.get("acq_epoch")
            t0 = ee.Algorithms.If(
                acq_epoch,
                ee.Date(ee.Number(acq_epoch).multiply(1000)),
                ee.Date(img.get("system:time_start")),
            )
            return f.set({
                "acq_millis": ee.Date(t0).millis(),
                "confidence_val": f.get("confidence"),
                "frp_val": f.get("frp"),
            })

        return sampled.map(_por_feature)

    return ee.FeatureCollection(ic.map(_por_imagen)).flatten()


def _hotspots_de_fuente(source: str, collection_id: str, scale: int,
                         region: "ee.Geometry", start: "ee.Date", end: "ee.Date",
                         limite: int) -> pd.DataFrame | None:
    pts = _puntos_de_coleccion(collection_id, scale, region, start, end)
    # ImageCollection.map().flatten() conserva el orden cronologico ASCENDENTE de la coleccion
    # (mas antiguo primero) - en dias de mucha actividad (>limite puntos en la ventana), el
    # .limit() de abajo se quedaba con los puntos MAS ANTIGUOS y descartaba justo las detecciones
    # mas recientes, las que mas importan para saber si un incendio sigue activo ahora mismo.
    # Se ordena por fecha descendente ANTES de truncar para que, si hay que perder puntos, se
    # pierdan los mas viejos.
    pts = pts.sort("acq_millis", False)
    try:
        info = pts.limit(limite).getInfo()
    except Exception as e:
        # antes se tragaba cualquier error en silencio (una fuente entera desaparecia del
        # ranking sin ningun aviso) - un print aqui al menos deja rastro en los logs del
        # servidor si GEE vuelve a fallar (p.ej. si algun dia se supera de nuevo el limite de
        # 5000 elementos al ordenar, ver comentario en obtener_hotspots_gee)
        print(f"[gee_hotspots] fallo consultando {source} ({collection_id}): {type(e).__name__}: {e}")
        return None
    feats = info.get("features", [])
    if not feats:
        return None
    rows = []
    for f in feats:
        props = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        rows.append({
            "latitude": lat,
            "longitude": lon,
            "acq_datetime": pd.to_datetime(props["acq_millis"], unit="ms", utc=True),
            "confidence": props.get("confidence_val"),
            "frp": props.get("frp_val"),
            "source": source,
        })
    return pd.DataFrame(rows)


def obtener_hotspots_gee(bbox: tuple[float, float, float, float],
                          window_hours: int = WINDOW_HOURS_DEFAULT,
                          limite_por_fuente: int = 4000) -> pd.DataFrame:
    """bbox = (west, south, east, north). Devuelve hotspots de las ultimas `window_hours`
    horas dentro de bbox, combinando VIIRS NOAA-20 + SNPP + FIRMS (equivalente Python del
    script GEE original, sin pasar por la interfaz de Code Editor).

    `limite_por_fuente` (por fuente, no total) se subio de 2000 a 4000: en dias de mucha
    actividad de incendios en España, una sola fuente (VIIRS) puede superar los 2000 puntos en
    72h, y aunque _hotspots_de_fuente() ya ordena por fecha descendente antes de truncar (para
    no perder las detecciones mas recientes), un limite demasiado bajo seguia cortando antes de
    llegar a las ultimas horas si esas ultimas horas por si solas ya superaban el limite. No
    subir de ~4500: Earth Engine aborta con EEException ("Collection query aborted after
    accumulating over 5000 elements") si sort()+limit() necesita acumular mas de 5000 elementos
    para poder ordenar - probado empiricamente, ver src/gee_hotspots.py commit que introduce
    este comentario."""
    _init()
    west, south, east, north = bbox
    region = ee.Geometry.Rectangle([west, south, east, north])
    end = ee.Date(pd.Timestamp.utcnow().isoformat())
    start = end.advance(-window_hours, "hour")

    # las 3 fuentes son independientes entre si - cada .getInfo() es una llamada de red
    # bloqueante a GEE, lanzarlas en paralelo reduce el tiempo total de "suma de las 3
    # latencias" a "la mayor de las 3"
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as executor:
        futures = [
            executor.submit(_hotspots_de_fuente, source, collection_id, scale,
                             region, start, end, limite_por_fuente)
            for source, (collection_id, scale) in SOURCES.items()
        ]
        resultados = [f.result() for f in futures]
    frames = [r for r in resultados if r is not None]

    if not frames:
        return pd.DataFrame(columns=["latitude", "longitude", "acq_datetime", "confidence", "frp", "source"])

    df = pd.concat(frames, ignore_index=True)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=window_hours)
    df = df[df["acq_datetime"] >= cutoff]
    return df.drop_duplicates(subset=["latitude", "longitude", "acq_datetime", "source"]).reset_index(drop=True)
