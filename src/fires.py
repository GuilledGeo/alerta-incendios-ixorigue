import time

import numpy as np
import pandas as pd
import requests
from sklearn.cluster import DBSCAN

from .config import CARDINAL_ES
from .geo_utils import RADIO_TIERRA_KM, bearing_deg, bearing_to_cardinal, haversine_km

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"


def identificar_incendios(hotspots: pd.DataFrame, eps_km: float = 5.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Agrupa hotspots individuales en 'incendios' (DBSCAN sobre haversine, eps_km = distancia
    maxima entre detecciones del mismo incendio). Devuelve (hotspots_con_fire_id, incendios_agg).

    `primera_deteccion`/`duracion_horas` estan acotadas por la ventana de consulta (WINDOW_HOURS):
    si el incendio empezo antes de esa ventana, la duracion real es mayor a la que aqui se calcula
    - se marca explicitamente en el mensaje, no se presenta como fecha de inicio real del incendio."""
    if hotspots.empty:
        vacio = pd.DataFrame(columns=[
            "fire_id", "lat", "lon", "n_detecciones", "primera_deteccion", "ultima_deteccion",
            "duracion_horas", "fuentes", "localidad", "municipio", "provincia",
        ])
        return hotspots.assign(fire_id=pd.Series(dtype=int)), vacio

    coords = np.radians(hotspots[["latitude", "longitude"]].to_numpy())
    eps_rad = eps_km / RADIO_TIERRA_KM
    labels = DBSCAN(eps=eps_rad, min_samples=1, metric="haversine").fit_predict(coords)

    hotspots = hotspots.copy()
    hotspots["fire_id"] = labels

    filas = []
    for fire_id, grupo in hotspots.groupby("fire_id"):
        primera, ultima = grupo["acq_datetime"].min(), grupo["acq_datetime"].max()
        filas.append({
            "fire_id": int(fire_id),
            "lat": grupo["latitude"].mean(),
            "lon": grupo["longitude"].mean(),
            "n_detecciones": len(grupo),
            "primera_deteccion": primera,
            "ultima_deteccion": ultima,
            "duracion_horas": (ultima - primera).total_seconds() / 3600.0,
            "fuentes": ", ".join(sorted(grupo["source"].unique())),
        })
    incendios = pd.DataFrame(filas).sort_values("n_detecciones", ascending=False).reset_index(drop=True)
    return hotspots, incendios


def geocodificar_incendios(incendios: pd.DataFrame) -> pd.DataFrame:
    """Reverse geocoding (Nominatim/OSM, gratuito, 1 req/s) del centroide de cada incendio -
    se hace solo sobre los clusters agregados (pocos), no sobre cada hotspot individual."""
    if incendios.empty:
        return incendios.assign(localidad="", municipio="", provincia="")

    incendios = incendios.copy()
    localidades, municipios, provincias, country_codes = [], [], [], []
    for _, fila in incendios.iterrows():
        localidad = municipio = provincia = country_code = ""
        try:
            r = requests.get(
                NOMINATIM_URL,
                params={"lat": fila["lat"], "lon": fila["lon"], "format": "json", "zoom": 10, "addressdetails": 1},
                headers={"User-Agent": "ixorigue-alerta-incendios/1.0"},
                timeout=10,
            )
            r.raise_for_status()
            addr = r.json().get("address", {})
            localidad = addr.get("village") or addr.get("town") or addr.get("city") or addr.get("hamlet") or ""
            municipio = addr.get("municipality") or addr.get("county") or ""
            provincia = addr.get("state") or addr.get("province") or ""
            country_code = (addr.get("country_code") or "").lower()
        except Exception:
            pass
        localidades.append(localidad)
        municipios.append(municipio)
        provincias.append(provincia)
        country_codes.append(country_code)
        time.sleep(1)  # respeta el limite de 1 req/s de Nominatim

    incendios["localidad"] = localidades
    incendios["municipio"] = municipios
    incendios["provincia"] = provincias
    incendios["country_code"] = country_codes
    return incendios


def excluir_paises(hotspots: pd.DataFrame, incendios: pd.DataFrame,
                    pais_permitido: str = "es") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Descarta incendios (y sus hotspots) geocodificados fuera de `pais_permitido` - la caja de
    consulta a GEE se expande +0.2 grados (~20 km) sobre el bbox de ranchos ES para no perder focos
    en el borde, lo que capta incendios reales de paises vecinos (Marruecos y Argelia via Estrecho/
    Mediterraneo, Portugal, Francia, Andorra) que no son relevantes para clientes ES. Se usa una
    lista blanca (solo 'es') en vez de negra, porque el vecindario relevante no se limita a un pais.
    country_code vacio (geocoding fallido) se mantiene, para no descartar focos por error de red."""
    if incendios.empty or "country_code" not in incendios.columns:
        return hotspots, incendios
    fuera = incendios[
        (incendios["country_code"] != "") & (incendios["country_code"] != pais_permitido)
    ]["fire_id"]
    if fuera.empty:
        return hotspots, incendios
    incendios = incendios[~incendios["fire_id"].isin(fuera)].reset_index(drop=True)
    hotspots = hotspots[~hotspots["fire_id"].isin(fuera)].reset_index(drop=True)
    return hotspots, incendios


def calcular_avance(hotspots: pd.DataFrame, velocidad_min_kmh: float = 0.15) -> pd.DataFrame:
    """Estima tendencia de avance (direccion + velocidad) de cada incendio comparando el centroide
    del primer tercio de detecciones (por tiempo) con el del ultimo tercio, dentro del mismo
    fire_id. Es una aproximacion gruesa a partir de donde ha ido cayendo cada pasada satelital,
    NO un modelo de propagacion de incendios (viento, combustible, pendiente...) - solo fiable con
    varias detecciones repartidas en el tiempo, por eso exige un minimo de puntos y de movimiento
    neto (el ruido de localizacion de VIIRS es de ~375m, y de FIRMS/MODIS de hasta 1km)."""
    filas = []
    for fire_id, grupo in hotspots.groupby("fire_id"):
        grupo = grupo.sort_values("acq_datetime")
        n = len(grupo)
        sin_datos = {"fire_id": fire_id, "direccion_avance": None, "direccion_avance_es": None,
                     "velocidad_kmh": None, "avance_confiable": False}

        duracion_h = (grupo["acq_datetime"].iloc[-1] - grupo["acq_datetime"].iloc[0]).total_seconds() / 3600.0
        if n < 3 or duracion_h < 1.0:
            filas.append(sin_datos)
            continue

        corte = max(1, n // 3)
        primero, ultimo = grupo.iloc[:corte], grupo.iloc[-corte:]
        lat0, lon0 = primero["latitude"].mean(), primero["longitude"].mean()
        lat1, lon1 = ultimo["latitude"].mean(), ultimo["longitude"].mean()
        horas_efectivas = (ultimo["acq_datetime"].mean() - primero["acq_datetime"].mean()).total_seconds() / 3600.0
        dist_km = haversine_km(lat0, lon0, lat1, lon1)

        if horas_efectivas < 0.5:
            filas.append(sin_datos)
            continue
        velocidad = dist_km / horas_efectivas
        if velocidad < velocidad_min_kmh:
            # desplazamiento neto por debajo del ruido tipico de localizacion del sensor -
            # no se puede distinguir de un incendio estacionario
            filas.append(sin_datos)
            continue

        cardinal = bearing_to_cardinal(bearing_deg(lat0, lon0, lat1, lon1))
        filas.append({
            "fire_id": fire_id, "direccion_avance": cardinal,
            "direccion_avance_es": CARDINAL_ES.get(cardinal, cardinal),
            "velocidad_kmh": velocidad, "avance_confiable": True,
        })

    return pd.DataFrame(filas)


def formatear_duracion(horas: float) -> str:
    if horas < 1:
        return f"{int(horas * 60)} min"
    if horas < 48:
        return f"{horas:.0f} h"
    return f"{horas / 24:.1f} días"


def formatear_hace(momento: pd.Timestamp) -> str:
    """'hace X h Y min' (o solo minutos si es menos de 1h) desde `momento` hasta ahora - para
    que el lector del informe PDF sepa de un vistazo la antiguedad real de una fecha/hora
    absoluta sin tener que restar mentalmente contra la hora de generacion del informe."""
    if pd.isna(momento):
        return ""
    delta_h = (pd.Timestamp.now(tz="UTC") - momento).total_seconds() / 3600.0
    if delta_h < 0:
        return "hace instantes"
    horas, minutos = divmod(int(round(delta_h * 60)), 60)
    if horas == 0:
        return f"hace {minutos} min"
    if minutos == 0:
        return f"hace {horas} h"
    return f"hace {horas} h {minutos} min"
