import time

import pandas as pd
import requests

# reintentos ante fallos transitorios de red/API - un informe para cliente no deberia quedarse
# sin datos meteorologicos por un timeout puntual de Open-Meteo, que en la practica ocurre de vez
# en cuando (servicio gratuito, sin SLA)
_INTENTOS = 3
_ESPERA_ENTRE_INTENTOS_S = 1.5


def obtener_meteo_reciente(lat: float, lon: float, horas: int = 6) -> pd.DataFrame:
    """Viento (direccion/velocidad/rafaga) + temperatura/humedad de las ultimas `horas` horas
    completas, via la API forecast de Open-Meteo (sin API key) con `past_days` - mismo patron
    que thi_ganaderias/src/weather.py:obtener_temp_humedad_horaria_reciente, sin depender de BD
    ni credenciales, por lo que funciona igual en el despliegue publico que en local. Humedad
    baja + viento fuerte es el combo que mas favorece la propagacion de un incendio, de ahi
    pedir ambos datos en la misma llamada para el informe.

    `direccion_grados` es de donde viene el viento (convencion meteorologica estandar: 0=N,
    90=E...) - para saber hacia donde SOPLA (y por tanto hacia donde empuja el fuego) hay que
    sumarle 180 grados.
    """
    error = None
    for intento in range(_INTENTOS):
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "winddirection_10m,windspeed_10m,wind_gusts_10m,temperature_2m,relative_humidity_2m,precipitation",
                    "past_days": 1,
                    "forecast_days": 1,
                    "timezone": "Europe/Madrid",
                },
                timeout=20,
            )
            r.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            error = e
            if intento < _INTENTOS - 1:
                time.sleep(_ESPERA_ENTRE_INTENTOS_S)
    else:
        raise error

    hourly = r.json()["hourly"]
    df = pd.DataFrame(hourly).rename(columns={
        "time": "fecha_hora",
        "winddirection_10m": "direccion_grados",
        "windspeed_10m": "velocidad_kmh",
        "wind_gusts_10m": "rafaga_kmh",
        "temperature_2m": "temp_c",
        "relative_humidity_2m": "humedad_pct",
        "precipitation": "precipitacion_mm",
    })
    df["fecha_hora"] = pd.to_datetime(df["fecha_hora"])
    # "ahora" en hora LOCAL DE MADRID explicitamente (no en la zona horaria del sistema donde
    # corre el proceso) - Open-Meteo devuelve fecha_hora como hora local de Madrid sin tz
    # adjunta (se pidio timezone=Europe/Madrid), y un servidor desplegado (Streamlit Cloud,
    # normalmente en UTC) tiene una hora de sistema distinta a la de Madrid: comparar
    # directamente contra pd.Timestamp.now() sin fijar la zona desalineaba el filtro y podia
    # devolver un DataFrame vacio (o con las horas mas recientes de menos) segun la zona horaria
    # del servidor, no de la ubicacion real de la finca.
    ahora_madrid = pd.Timestamp.now(tz="Europe/Madrid").tz_localize(None).floor("h")
    df = df[df["fecha_hora"] <= ahora_madrid].tail(horas).reset_index(drop=True)
    return df
