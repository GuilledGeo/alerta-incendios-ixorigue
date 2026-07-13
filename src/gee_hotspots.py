import os

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


def obtener_hotspots_gee(bbox: tuple[float, float, float, float],
                          window_hours: int = WINDOW_HOURS_DEFAULT,
                          limite_por_fuente: int = 2000) -> pd.DataFrame:
    """bbox = (west, south, east, north). Devuelve hotspots de las ultimas `window_hours`
    horas dentro de bbox, combinando VIIRS NOAA-20 + SNPP + FIRMS (equivalente Python del
    script GEE original, sin pasar por la interfaz de Code Editor)."""
    _init()
    west, south, east, north = bbox
    region = ee.Geometry.Rectangle([west, south, east, north])
    end = ee.Date(pd.Timestamp.utcnow().isoformat())
    start = end.advance(-window_hours, "hour")

    frames = []
    for source, (collection_id, scale) in SOURCES.items():
        pts = _puntos_de_coleccion(collection_id, scale, region, start, end)
        try:
            info = pts.limit(limite_por_fuente).getInfo()
        except Exception:
            continue
        feats = info.get("features", [])
        if not feats:
            continue
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
        frames.append(pd.DataFrame(rows))

    if not frames:
        return pd.DataFrame(columns=["latitude", "longitude", "acq_datetime", "confidence", "frp", "source"])

    df = pd.concat(frames, ignore_index=True)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=window_hours)
    df = df[df["acq_datetime"] >= cutoff]
    return df.drop_duplicates(subset=["latitude", "longitude", "acq_datetime", "source"]).reset_index(drop=True)
