"""Texto interpretativo en lenguaje llano para el informe PDF - el publico objetivo (el
ganadero/cliente) no es tecnico, asi que en vez de solo mostrar numeros se deducen y explican
las conclusiones (¿esta rodeado por el fuego?, ¿el viento empuja el fuego hacia la finca?,
¿avanza rapido?). Las funciones devuelven piezas de texto por separado (no un parrafo unico)
para que src/pdf_report.py pueda darle formato distinto a cada una (negrita al hecho principal,
cursiva a los matices) en vez de un bloque de texto plano."""
import math

import pandas as pd

from .fires import formatear_hace
from .geo_utils import bearing_deg


def interpretar_riesgo(aviso_row, hotspots_mismo_fuego: pd.DataFrame | None = None) -> list[str]:
    """Piezas de texto sobre el riesgo actual: el mensaje ya generado en evaluar_riesgo() (misma
    redaccion que ve el operador en la app, para no duplicar criterio), la matizacion de que un
    foco "antiguo" cerca de la finca no significa que el incendio este controlado si el mismo
    incendio sigue teniendo detecciones recientes en otros puntos, y el aviso de que los focos
    "controlados" pueden reactivarse."""
    partes = [aviso_row["mensaje_final"]]

    if hotspots_mismo_fuego is not None and not hotspots_mismo_fuego.empty:
        ultima_deteccion_fuego = hotspots_mismo_fuego["acq_datetime"].max()
        ultima_deteccion_finca = aviso_row["acq_datetime"]
        # mas de 1h de diferencia: hay actividad del mismo incendio mas reciente que la que
        # disparo este aviso concreto, en otro punto del incendio (no necesariamente cerca)
        if (ultima_deteccion_fuego - ultima_deteccion_finca).total_seconds() > 3600:
            ts = ultima_deteccion_fuego.tz_convert("Europe/Madrid").strftime("%d/%m %H:%M %Z")
            partes.append(
                f"Aunque el foco más cercano a su finca lleva un tiempo sin nueva actividad, "
                f"este mismo incendio sigue registrando detecciones más recientes en otros "
                f"puntos (la última, a fecha de {ts}, {formatear_hace(ultima_deteccion_fuego)}): "
                f"el incendio en su conjunto sigue activo, no está controlado."
            )

    partes.append(
        "Un foco sin nuevas detecciones durante varias horas no equivale a \"apagado\": los "
        "focos catalogados como controlados pueden reactivarse, especialmente si el viento o la "
        "vegetación seca lo favorecen."
    )
    return partes


def detectar_rodeado(rancho_geom, hotspots_cercanos: pd.DataFrame) -> tuple[bool, str]:
    """Calcula el rumbo desde el centroide de la finca a cada hotspot cercano y mide el mayor
    hueco angular entre focos consecutivos (ordenados). Un hueco pequeño significa que hay
    focos repartidos en casi todo el compas alrededor de la finca (riesgo de quedar rodeado);
    un hueco grande significa que estan concentrados en una sola direccion."""
    if hotspots_cercanos is None or hotspots_cercanos.empty:
        return False, ""

    centroide = rancho_geom.centroid
    rumbos = sorted(
        bearing_deg(centroide.y, centroide.x, hs["latitude"], hs["longitude"])
        for _, hs in hotspots_cercanos.iterrows()
    )

    if len(rumbos) == 1:
        return False, (
            "Los focos detectados están concentrados en una única dirección respecto a su "
            "finca, no hay indicios de que el fuego la esté rodeando por varios frentes."
        )

    huecos = [(rumbos[i + 1] - rumbos[i]) for i in range(len(rumbos) - 1)]
    huecos.append(360 - rumbos[-1] + rumbos[0])  # hueco que cierra el circulo
    hueco_max = max(huecos)

    # umbral: si el mayor hueco sin focos es menor a 100 grados, los focos cubren mas de 260
    # grados del compas alrededor de la finca - repartidos en varios frentes, no en uno solo
    if hueco_max < 100:
        return True, (
            "Los focos detectados están repartidos en varias direcciones alrededor de su "
            "finca, no concentrados en un único frente: existe riesgo de quedar rodeado si los "
            "distintos focos avanzan hacia usted a la vez. Esté especialmente alerta."
        )
    return False, (
        "Los focos detectados están concentrados en una zona del entorno de su finca, no "
        "repartidos por varios frentes: de momento no hay indicios de quedar rodeado por el "
        "fuego."
    )


_UMBRALES_VELOCIDAD = [
    (10, "suave", "no debería acelerar significativamente el avance del fuego"),
    (25, "moderado", "puede acelerar el avance del fuego en la dirección hacia la que sopla"),
    (float("inf"), "fuerte",
     "puede acelerar considerablemente el avance del fuego y dificultar su control"),
]


def interpretar_viento(meteo_df: pd.DataFrame, bearing_foco_a_finca: float | None) -> dict:
    """Direccion/velocidad media reciente del viento (media vectorial ponderada por velocidad,
    para no promediar mal valores cerca de 0/360 grados), comparada con el rumbo DESDE EL FOCO
    HACIA LA FINCA: si el viento sopla en esa misma direccion (+/-45 grados), puede empujar el
    fuego hacia la finca.

    Devuelve un dict con las piezas de texto por separado (para poder darles formato distinto
    en el PDF) y los valores numericos ya calculados (para dibujar el radar de viento sin
    recalcular la direccion dos veces).
    """
    resultado = {
        "frase_velocidad": None, "frase_direccion": None, "direccion_sopla_hacia": None,
        "velocidad_media": None, "velocidad_max": None, "alineado": False,
    }
    if meteo_df is None or meteo_df.empty:
        resultado["frase_velocidad"] = "No se han podido obtener datos de viento recientes para esta zona."
        return resultado

    velocidades = meteo_df["velocidad_kmh"]
    rad = meteo_df["direccion_grados"].apply(math.radians)
    x = (velocidades * rad.apply(math.cos)).sum()
    y = (velocidades * rad.apply(math.sin)).sum()
    direccion_viene_de = math.degrees(math.atan2(y, x)) % 360
    direccion_sopla_hacia = (direccion_viene_de + 180) % 360
    velocidad_media = velocidades.mean()
    velocidad_max = velocidades.max()

    _, categoria, consecuencia = next(
        (umbral, cat, cons) for umbral, cat, cons in _UMBRALES_VELOCIDAD if velocidad_media < umbral
    )

    resultado["frase_velocidad"] = (
        f"En las últimas horas el viento en la zona ha sido {categoria} "
        f"(~{velocidad_media:.0f} km/h de media, rachas de hasta {velocidad_max:.0f} km/h): "
        f"{consecuencia}."
    )
    resultado["direccion_sopla_hacia"] = direccion_sopla_hacia
    resultado["velocidad_media"] = velocidad_media
    resultado["velocidad_max"] = velocidad_max

    if bearing_foco_a_finca is None:
        return resultado

    diferencia = abs((direccion_sopla_hacia - bearing_foco_a_finca + 180) % 360 - 180)
    resultado["alineado"] = diferencia <= 45
    if resultado["alineado"]:
        resultado["frase_direccion"] = (
            "El viento sopla en la dirección del foco hacia su finca: si el incendio avanza "
            "empujado por el viento, esta sería la dirección más probable de avance hacia usted."
        )
    else:
        resultado["frase_direccion"] = (
            "En las últimas horas el viento no ha soplado predominantemente desde el foco hacia "
            "su finca, aunque la dirección del viento puede cambiar a lo largo del día."
        )
    return resultado
