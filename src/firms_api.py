"""Cliente de la Area API de NASA FIRMS (https://firms.modaps.eosdis.nasa.gov/api/area/),
alternativa a las colecciones NRT de Google Earth Engine (src/gee_hotspots.py) para hotspots mas
frescos: FIRMS publica sus productos NRT en menos de 3h desde la pasada del satelite (a nivel
global, incluida España), mientras que el espejo de esas mismas colecciones en Earth Engine tiene
en la practica un retraso observado de 24-40h (ver investigacion de 2026-07-27 en el historial de
este repo) - GEE no reingesta el catalogo NRT con la misma frecuencia que la fuente original.

Requiere una MAP_KEY gratuita (variable de entorno FIRMS_MAP_KEY): se solicita sin coste en
https://firms.modaps.eosdis.nasa.gov/api/map_key/ con solo un email. Limite de la API: 5000
transacciones/10 minutos por MAP_KEY (una consulta de area consume 1 transaccion salvo areas muy
grandes).

Este modulo es un cliente independiente, no esta conectado todavia al pipeline principal de la
app (src/gee_hotspots.py sigue siendo la fuente usada por app.py) - hay que verificar con datos
reales (una MAP_KEY valida) que la latencia observada aqui es realmente mejor antes de decidir si
sustituye o complementa a GEE."""
import io
import os

import pandas as pd
import requests

# fuentes NRT globales (no limitadas a EEUU/Canada, a diferencia de LANDSAT_NRT o las variantes
# URT) - equivalentes a las 3 fuentes que ya se consultan via GEE en src/gee_hotspots.py
FIRMS_SOURCES = ["VIIRS_NOAA20_NRT", "VIIRS_SNPP_NRT", "MODIS_NRT"]

AREA_API_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"


def _parse_acq_datetime(df: pd.DataFrame) -> pd.Series:
    """acq_date viene en formato YYYY-MM-DD y acq_time en formato HHMM (0-2359, sin ceros a la
    izquierda: "1" es 00:01 UTC, "2359" es 23:59 UTC) - hay que rellenar con zfill(4) antes de
    partir en horas/minutos."""
    acq_time = df["acq_time"].astype(int).astype(str).str.zfill(4)
    return pd.to_datetime(
        df["acq_date"].astype(str) + " " + acq_time.str[:2] + ":" + acq_time.str[2:],
        utc=True,
    )


def _hotspots_de_fuente_firms(map_key: str, source: str, bbox: tuple[float, float, float, float],
                               dias: int) -> pd.DataFrame | None:
    west, south, east, north = bbox
    area = f"{west},{south},{east},{north}"
    url = f"{AREA_API_BASE}/{map_key}/{source}/{area}/{dias}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    texto = r.text.strip()

    # la API no devuelve un error HTTP si la MAP_KEY es invalida o se supera el limite de
    # transacciones - devuelve un texto plano de una linea en vez de CSV (p.ej. "Invalid MAP_KEY"
    # o "Error: too many transactions"). Si no empieza por una cabecera CSV reconocible, se trata
    # como error explicito en vez de dejar que read_csv falle con un mensaje confuso
    primera_linea = texto.split("\n", 1)[0].lower()
    if "latitude" not in primera_linea and "country_id" not in primera_linea:
        raise RuntimeError(f"FIRMS Area API ({source}) devolvio un error en vez de CSV: {texto[:200]}")

    df = pd.read_csv(io.StringIO(texto))
    if df.empty:
        return None

    df = df.copy()
    df["acq_datetime"] = _parse_acq_datetime(df)
    df["source"] = f"FIRMS_API_{source}"
    for columna in ("confidence", "frp"):
        if columna not in df.columns:
            df[columna] = None
    return df[["latitude", "longitude", "acq_datetime", "confidence", "frp", "source"]]


def obtener_hotspots_firms_api(bbox: tuple[float, float, float, float], dias: int = 3,
                                map_key: str | None = None) -> pd.DataFrame:
    """bbox = (west, south, east, north). Devuelve hotspots de los ultimos `dias` (1-5, limite de
    la Area API) dentro de bbox, combinando VIIRS NOAA-20 + SNPP + MODIS directamente desde FIRMS
    (sin pasar por Earth Engine) - mismo esquema de columnas que
    src.gee_hotspots.obtener_hotspots_gee(), para poder usarse como sustituto o complemento sin
    tocar el resto del pipeline (identificar_incendios, evaluar_riesgo...)."""
    map_key = map_key or os.getenv("FIRMS_MAP_KEY")
    if not map_key:
        raise ValueError(
            "Falta FIRMS_MAP_KEY (variable de entorno o parametro map_key) - solicitar una "
            "gratis en https://firms.modaps.eosdis.nasa.gov/api/map_key/"
        )
    dias = min(max(dias, 1), 5)  # la Area API solo admite un rango de 1 a 5 dias por consulta

    frames = []
    for source in FIRMS_SOURCES:
        df = _hotspots_de_fuente_firms(map_key, source, bbox, dias)
        if df is not None:
            frames.append(df)

    if not frames:
        return pd.DataFrame(columns=["latitude", "longitude", "acq_datetime", "confidence", "frp", "source"])

    return pd.concat(frames, ignore_index=True).drop_duplicates(
        subset=["latitude", "longitude", "acq_datetime", "source"]
    ).reset_index(drop=True)
