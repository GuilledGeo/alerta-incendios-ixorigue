import geopandas as gpd
import pandas as pd
from pyproj import Geod
from shapely.geometry import Point, box
from shapely.ops import nearest_points

from .config import CARDINAL_ES, RING_RISK, RING_THRESHOLDS_KM
from .fires import formatear_duracion, formatear_hace
from .geo_utils import bearing_deg, bearing_to_cardinal

# EPSG:3857 (Web Mercator) NO es equidistante: a la latitud de España (~36-43N) infla las
# distancias medidas directamente sobre sus coordenadas en ~un 25-38% (factor 1/cos(latitud)) -
# bug real detectado el 29-jul-2026 comparando con una medicion manual (un foco a 7.96 km reales
# salia calculado como 10.45 km, sacando de la lista de riesgo una ganaderia que si estaba dentro
# del perimetro de 10 km). EPSG:25830 (ETRS89 / UTM huso 30N, el de referencia oficial para la
# España peninsular) tiene una distorsion de <1% para estas distancias cortas - se usa aqui para
# el filtro espacial (sjoin) y como base antes de refinar con la distancia geodesica exacta
# (Geod.inv, sin proyeccion, la unica realmente libre de distorsion en cualquier punto de España
# incluidas Canarias/Baleares) que se calcula en evaluar_riesgo() para el numero final reportado.
CRS_METRICO = "EPSG:25830"
_GEOD = Geod(ellps="WGS84")

# umbrales (horas desde la ultima deteccion satelital del foco) para diferenciar los avisos
# que siguen activos de los que probablemente ya estan controlados/extinguidos - compartido
# entre app.py (badge del ranking) y src/pdf_report.py (informe PDF).
#
# 24h/48h: deliberadamente los MISMOS cortes que ya usa la leyenda de antiguedad de los hotspots
# en el mapa (config.HOTSPOT_AGE_BINS_H = [6, 12, 24, 48, 72] - un punto pasa de naranja
# "12-24h" a amarillo "24-48h" justo en las 24h, y de amarillo a verde "48-72h" justo en las
# 48h). Antes este modulo tenia su propia escala independiente (24h/36h, luego 12h/24h en un
# primer intento) que no coincidia con ninguno de esos cortes del mapa - resultaba en que el
# color del punto y el estado del foco cambiaban en momentos distintos sin motivo, confuso para
# quien mira ambos a la vez. Usar los mismos cortes que el mapa es lo que de verdad da
# coherencia, no un numero concreto en si mismo.
ESTADO_FOCO_ACTIVO_H = 24
ESTADO_FOCO_CONTROLADO_H = 48


def estado_foco(ultima_deteccion) -> tuple[str, str, str]:
    """(emoji+label, color, texto corto) segun antiguedad de la ultima deteccion del foco:
    <24h = activo, 24-48h = en seguimiento (zona gris entre ambos umbrales), >=48h = controlado."""
    if pd.isna(ultima_deteccion):
        return "", "#6b7280", ""
    horas = (pd.Timestamp.now(tz="UTC") - ultima_deteccion).total_seconds() / 3600.0
    if horas < ESTADO_FOCO_ACTIVO_H:
        return "🔥 Foco activo", "#ef4444", "Activo"
    if horas >= ESTADO_FOCO_CONTROLADO_H:
        return "✅ Foco controlado", "#22c55e", "Controlado"
    return "🟡 En seguimiento", "#eab308", "En seguimiento"


ANILLOS_LABELS = ["0-3 km", "3-5 km", "5-10 km"]  # deben coincidir en orden con RING_THRESHOLDS_KM


def anillos_riesgo(rancho_geom):
    """Buffers del perimetro del rancho a 3/5/10 km (zonas de seguridad), en el mismo sistema de
    distancias que usa evaluar_riesgo() (distancia al perimetro, no al centroide). Compartida
    entre app.py (mini-mapa Folium) y src/pdf_report.py (mapa estatico del informe PDF).

    Proyeccion azimutal equidistante CENTRADA EN EL PROPIO RANCHO (no CRS_METRICO/UTM fijo) -
    por construccion, las distancias medidas DESDE EL CENTRO de esta proyeccion son exactas en
    cualquier punto de España (Canarias incluidas), sin el ~1% de distorsion residual que
    tendria un huso UTM fijo lejos de su meridiano central. El ranch_geom real puede estar a
    unos pocos km de su propio centroide (el punto que centra la proyeccion), un margen de
    error totalmente despreciable para radios de 3-10 km."""
    centro = rancho_geom.centroid
    crs_local = f"+proj=aeqd +lat_0={centro.y} +lon_0={centro.x} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    base_m = gpd.GeoSeries([rancho_geom], crs="EPSG:4326").to_crs(crs_local)
    return [
        (label, base_m.buffer(km * 1000).to_crs("EPSG:4326").iloc[0])
        for km, label in zip(RING_THRESHOLDS_KM, ANILLOS_LABELS)
    ]


# aspect_ratio (ancho/alto) del mapa "vista general" del informe PDF (src/pdf_report.py) - vive
# aqui (no en pdf_report.py) para que bbox_vista_general() pueda usar exactamente el mismo valor
# al calcular que hotspots caen dentro del area visible de ese mapa, sin duplicar la constante.
# Bajado de 2.2 a 1.7 (mas cuadrado) para que el mapa se vea mas grande/prominente al insertarse
# a todo el ancho de la pagina - con 2.2 quedaba muy achatado (poca altura) aunque ocupara el
# ancho completo.
MAPA_GENERAL_ASPECT = 1.7


def bbox_vista_general(rancho_geom, aspect_ratio: float = MAPA_GENERAL_ASPECT, padding_frac: float = 0.08):
    """Bbox (minx, miny, maxx, maxy) en EPSG:4326 que cubre exactamente la misma area visible que
    el mapa de "vista general" del informe PDF (rectangulo alargado centrado en el anillo de
    10km) - se usa para filtrar que hotspots incluir en ese mapa, igual que el bbox del anillo de
    10km se usa para filtrar los hotspots "cercanos" del resto de mapas/narrativa. Sin esto, el
    mapa (mas ancho que el area de 10km) mostraria terreno sin ningun hotspot en los bordes
    aunque si hubiera focos activos ahi."""
    outer_ring_3857 = gpd.GeoSeries([anillos_riesgo(rancho_geom)[-1][1]], crs="EPSG:4326").to_crs(CRS_METRICO)
    minx, miny, maxx, maxy = outer_ring_3857.iloc[0].bounds
    dx, dy = maxx - minx, maxy - miny
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    lado_y = max(dx, dy) * (1 + 2 * padding_frac)
    lado_x = lado_y * aspect_ratio
    caja_3857 = gpd.GeoSeries(
        [box(cx - lado_x / 2, cy - lado_y / 2, cx + lado_x / 2, cy + lado_y / 2)], crs=CRS_METRICO,
    )
    return tuple(caja_3857.to_crs("EPSG:4326").iloc[0].bounds)

# columnas del DataFrame de avisos, usadas tambien para el caso "sin avisos": un pd.DataFrame()
# a secas no tiene columnas, y el codigo llamante (app.py) siempre indexa por nombre de columna
# (avisos["ranch_id"], avisos["risk_level"]...) incluso cuando no hay ninguna fila
COLUMNAS_AVISOS = [
    "ranch_id", "ranch_name", "customer_name", "customer_phone", "region", "tipo_ganaderia",
    "zona_nombre", "distance_km", "ring", "risk_level", "direction", "direction_es",
    "hotspot_lat", "hotspot_lon", "acq_datetime", "source", "fire_id", "localidad", "municipio",
    "provincia", "lugar", "duracion_horas", "n_detecciones_incendio", "ultima_deteccion",
    "direccion_avance_es", "velocidad_kmh", "avance_confiable", "mensaje_final",
]


def _ring_label(dist_km: float, dentro: bool) -> str:
    if dentro:
        return "Dentro (0 km)"
    if dist_km <= 3:
        return "0-3 km"
    if dist_km <= 5:
        return "3-5 km"
    if dist_km <= 10:
        return "5-10 km"
    return ">10 km"


def evaluar_riesgo(ranchos: gpd.GeoDataFrame, hotspots: pd.DataFrame) -> pd.DataFrame:
    """Para cada par (rancho, hotspot) dentro de 10 km: distancia al perimetro, direccion
    cardinal desde el centroide del rancho, anillo, nivel de riesgo y mensaje de aviso -
    mismo modelo que enriched.map(...) del script GEE original."""
    if hotspots.empty or ranchos.empty:
        return pd.DataFrame(columns=COLUMNAS_AVISOS)

    # reset_index es imprescindible: ranchos puede venir con un indice no contiguo (tras
    # filtrar en src/ranches.py los ranchos sin zona) - al construir mas abajo un GeoDataFrame
    # combinando un dict plano ("_ranch_idx": ranchos_m.index) con una geometry= que conserva
    # su propio indice, pandas alinea por etiqueta y desincroniza silenciosamente ambas columnas
    # (geometrias a None o emparejadas con el rancho equivocado) si el indice no es 0..n-1
    ranchos = ranchos.reset_index(drop=True)
    ranchos_m = ranchos.to_crs(CRS_METRICO)
    hotspots = hotspots.reset_index(drop=True)
    hotspots_geom = gpd.GeoSeries(
        [Point(lon, lat) for lon, lat in zip(hotspots["longitude"], hotspots["latitude"])],
        crs="EPSG:4326",
    ).to_crs(CRS_METRICO).reset_index(drop=True)

    # +10% de margen sobre el radio real: CRS_METRICO (EPSG:25830, UTM huso 30N) tiene una
    # distorsion residual pequeña pero no nula lejos de su meridiano central (p.ej. Canarias,
    # extremo este de Baleares) - sin este margen, el sjoin de abajo podria excluir por poco
    # algun par que si esta dentro de los 10 km reales. El filtro exacto de verdad (dist_km >
    # RING_THRESHOLDS_KM[-1]) sigue aplicandose despues con la distancia geodesica ya corregida,
    # este radio solo decide que candidatos entran a esa comprobacion, un falso positivo aqui no
    # tiene coste (se descarta enseguida), un falso negativo si (se perderia el aviso entero)
    radio_max_m = RING_THRESHOLDS_KM[-1] * 1000 * 1.1

    # candidate set via indice espacial (STRtree de GeoPandas) en vez de comparar cada rancho
    # contra cada hotspot en Python puro: sjoin de los hotspots contra el buffer de 10km de cada
    # rancho da exactamente los pares a <=10km del poligono real (no del centroide), el mismo
    # criterio que el filtro `dist_km > 10` de antes, sin recorrer el producto cartesiano completo
    ranchos_buffer = gpd.GeoDataFrame(
        {"_ranch_idx": ranchos_m.index}, geometry=ranchos_m.geometry.buffer(radio_max_m), crs=CRS_METRICO,
    )
    hotspots_puntos = gpd.GeoDataFrame(
        {"_hs_idx": hotspots.index}, geometry=hotspots_geom, crs=CRS_METRICO,
    )
    pares = gpd.sjoin(hotspots_puntos, ranchos_buffer, predicate="within", how="inner")

    filas = []
    for _, par in pares.iterrows():
        i, j = par["_ranch_idx"], par["_hs_idx"]
        rancho = ranchos_m.loc[i]
        hs = hotspots.loc[j]
        centroid_4326 = ranchos.geometry.loc[i].centroid
        hs_geom_m = hotspots_geom.loc[j]

        # distancia geodesica REAL (elipsoide WGS84, sin proyeccion) entre el punto del
        # poligono (borde de la zona, no el centroide) mas cercano al hotspot y el hotspot -
        # bug real corregido el 29-jul-2026: usar directamente rancho.geometry.distance() sobre
        # coordenadas proyectadas (antes EPSG:3857) da la distancia en las unidades DE ESA
        # PROYECCION, no en metros reales; a la latitud de España eso inflaba la distancia
        # hasta un ~31% (verificado con Web Mercator). Aqui solo se usa la proyeccion para
        # encontrar QUE punto del poligono es el mas cercano (una operacion geometrica, no
        # necesita ser exacta en distancia) - el numero que de verdad importa se recalcula
        # despues con Geod.inv(), la unica forma libre de distorsion en cualquier punto de España
        punto_cercano_m = nearest_points(rancho.geometry, hs_geom_m)[0]
        punto_cercano_4326 = gpd.GeoSeries([punto_cercano_m], crs=CRS_METRICO).to_crs("EPSG:4326").iloc[0]
        _, _, dist_m = _GEOD.inv(punto_cercano_4326.x, punto_cercano_4326.y, hs["longitude"], hs["latitude"])
        dist_km = dist_m / 1000.0
        if dist_km > RING_THRESHOLDS_KM[-1]:
            # el buffer poligonal usado para el sjoin es una aproximacion del circulo real
            # (segmentos rectos) - salvaguarda para no colar algun par justo en el borde
            continue
        dentro = rancho.geometry.contains(hs_geom_m)

        brng = bearing_deg(centroid_4326.y, centroid_4326.x, hs["latitude"], hs["longitude"])
        cardinal = bearing_to_cardinal(brng)
        ring = _ring_label(dist_km, dentro)
        riesgo = RING_RISK.get(ring, "1/3")
        ts_local = hs["acq_datetime"].tz_convert("Europe/Madrid").strftime("%Y-%m-%d %H:%M %Z")

        # info del incendio (cluster) al que pertenece este hotspot, si ya se calculo
        # (src/fires.py) - opcional, evaluar_riesgo tambien funciona sin ello
        localidad = hs.get("localidad") or ""
        municipio = hs.get("municipio") or ""
        provincia = hs.get("provincia") or ""
        duracion_h = hs.get("duracion_horas")
        n_detecciones = hs.get("n_detecciones")
        ultima_deteccion = hs.get("ultima_deteccion")
        direccion_avance_es = hs.get("direccion_avance_es")
        velocidad_kmh = hs.get("velocidad_kmh")
        avance_confiable = bool(hs.get("avance_confiable"))

        # dedupe: localidad y municipio a veces coinciden (p.ej. capital de municipio)
        partes_lugar = list(dict.fromkeys(p for p in [localidad, municipio, provincia] if p))
        lugar = ", ".join(partes_lugar)
        frase_lugar = f" en las inmediaciones de {lugar}" if lugar else ""
        frase_duracion = (
            f" El foco lleva activo al menos {formatear_duracion(duracion_h)} "
            f"dentro de la ventana de detección consultada."
            if pd.notna(duracion_h) else ""
        )
        frase_ultima = ""
        if pd.notna(ultima_deteccion) and ultima_deteccion != hs["acq_datetime"]:
            ultima_local = ultima_deteccion.tz_convert("Europe/Madrid").strftime("%Y-%m-%d %H:%M %Z")
            frase_ultima = f" Última actualización del foco: {ultima_local} ({formatear_hace(ultima_deteccion)})."
        frase_avance = ""
        if avance_confiable and direccion_avance_es and pd.notna(velocidad_kmh):
            frase_avance = (
                f" El incendio muestra una tendencia de avance hacia el {direccion_avance_es}, "
                f"a una velocidad aproximada de {velocidad_kmh:.1f} km/h (estimación orientativa "
                f"a partir de las detecciones satelitales, no un modelo de propagación real)."
            )

        mensaje = (
            f"Detectado posible incendio (punto caliente) a fecha de {ts_local} "
            f"({formatear_hace(hs['acq_datetime'])})"
            f"{frase_lugar}, a {dist_km:.1f} km de su posición, dirección {CARDINAL_ES[cardinal]}."
            f"{frase_duracion}{frase_ultima}{frase_avance} Esté alerta de su evolución. "
            f"Actualmente se encuentra en riesgo {riesgo} respecto a su finca."
        )

        filas.append({
            "ranch_id": rancho["ranch_id"],
            "ranch_name": rancho["ranch_name"],
            "customer_name": rancho["customer_name"],
            "customer_phone": rancho.get("customer_phone"),
            "region": rancho.get("region"),
            "tipo_ganaderia": rancho.get("tipo_ganaderia"),
            "zona_nombre": rancho.get("zona_nombre"),
            "distance_km": round(dist_km, 2),
            "ring": ring,
            "risk_level": riesgo,
            "direction": cardinal,
            "direction_es": CARDINAL_ES[cardinal],
            "hotspot_lat": hs["latitude"],
            "hotspot_lon": hs["longitude"],
            "acq_datetime": hs["acq_datetime"],
            "source": hs["source"],
            "fire_id": hs.get("fire_id"),
            "localidad": localidad,
            "municipio": municipio,
            "provincia": provincia,
            "lugar": lugar,
            "duracion_horas": duracion_h,
            "n_detecciones_incendio": n_detecciones,
            "ultima_deteccion": ultima_deteccion,
            "direccion_avance_es": direccion_avance_es,
            "velocidad_kmh": velocidad_kmh,
            "avance_confiable": avance_confiable,
            "mensaje_final": mensaje,
        })

    if not filas:
        return pd.DataFrame(columns=COLUMNAS_AVISOS)

    df = pd.DataFrame(filas)
    # un aviso por (rancho, hotspot mas cercano en el tiempo mas reciente) - evita duplicar
    # el mismo rancho varias veces si hay varios hotspots cercanos, nos quedamos con el peor caso
    orden_riesgo = {"3/3": 0, "2/3": 1, "1/3": 2}
    df["_orden"] = df["risk_level"].map(orden_riesgo)
    df = df.sort_values(["_orden", "distance_km"]).drop_duplicates("ranch_id").drop(columns="_orden")
    return df.reset_index(drop=True)
