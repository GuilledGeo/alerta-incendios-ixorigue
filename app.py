import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# fijar PROJ_LIB/PROJ_DATA ANTES de importar geopandas/pyproj (aqui, no en src/pdf_report.py:
# el contexto PROJ de pyproj se inicializa la primera vez que se usa, y cambiar la variable de
# entorno DESPUES no tiene efecto) - evita que otra instalacion de PROJ en el sistema (p.ej.
# PostgreSQL/PostGIS local, habitual en equipos de desarrollo) con un proj.db incompatible rompa
# silenciosamente las reproyecciones (el mapa estatico del informe PDF se quedaba sin imagen de
# satelite de fondo). Es un no-op inofensivo si no hay ningun conflicto que resolver.
try:
    import importlib.util
    # find_spec() localiza el modulo SIN ejecutarlo - un `import rasterio` normal ya dispara el
    # registro interno de GDAL/PROJ en ese mismo instante, demasiado pronto para poder corregir
    # la variable de entorno antes de que quede fijada
    _spec = importlib.util.find_spec("rasterio")
    _rasterio_proj_dir = Path(_spec.origin).resolve().parent / "proj_data" if _spec else None
    if _rasterio_proj_dir and _rasterio_proj_dir.exists():
        os.environ["PROJ_LIB"] = str(_rasterio_proj_dir)
        os.environ["PROJ_DATA"] = str(_rasterio_proj_dir)
except ImportError:
    pass

import folium
import geopandas as gpd
import pandas as pd
import streamlit as st
from folium.plugins import MeasureControl
from streamlit_folium import st_folium

# puente secrets.toml -> variables de entorno, ANTES de importar src.config/src.ranches (que
# leen esas variables al importarse). En local, sin .streamlit/secrets.toml, st.secrets lanza
# StreamlitSecretNotFoundError con solo mirarlo (no hay archivo que parsear) - se ignora, las
# credenciales personales (.env, `earthengine authenticate`) siguen funcionando igual.
try:
    for _clave in (
        "GEE_SERVICE_ACCOUNT_EMAIL", "GEE_SERVICE_ACCOUNT_KEY_JSON",
        "RANCHOS_DATA_SOURCE", "RANCHOS_SNAPSHOT_PATH", "FIRMS_MAP_KEY",
    ):
        if _clave in st.secrets:
            os.environ[_clave] = str(st.secrets[_clave])
except Exception:
    pass

from src.config import (
    COLORS_RING, HOTSPOT_AGE_BINS_H, HOTSPOT_AGE_COLORS,
    RANCHOS_DATA_SOURCE, RANCHOS_SNAPSHOT_PATH, RING_RISK, RING_THRESHOLDS_KM, WINDOW_HOURS_DEFAULT,
)

# el snapshot de ranchos (contiene datos de cliente) nunca va en este repo publico - si llega
# como secret en base64 (RANCHOS_SNAPSHOT_B64), se escribe aqui en disco antes de leerlo. Es el
# mecanismo pensado para Streamlit Community Cloud, que no tiene almacenamiento persistente
# propio ni acceso a la BD de produccion - ver .streamlit/secrets.toml.example
try:
    if "RANCHOS_SNAPSHOT_B64" in st.secrets:
        import base64
        RANCHOS_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RANCHOS_SNAPSHOT_PATH.write_bytes(base64.b64decode(st.secrets["RANCHOS_SNAPSHOT_B64"]))
except Exception:
    pass
from src.fires import calcular_avance, excluir_paises, formatear_duracion, geocodificar_incendios, identificar_incendios
from src.firms_api import obtener_hotspots_firms_api
from src.gee_hotspots import obtener_hotspots_gee
from src.pdf_report import generar_pdf_aviso
from src.ranches import obtener_ranchos_es, obtener_zonas_es
from src.risk import anillos_riesgo, bbox_vista_general, estado_foco, evaluar_riesgo

st.set_page_config(page_title="Alerta de incendios — Ixorigue", layout="wide", page_icon="🔥", initial_sidebar_state="collapsed")

COLOR_FUENTE = {
    "perimetro_dibujado": "#3b82f6",
    "union_de_zonas": "#22c55e",
}
DASH_FUENTE = {
    "perimetro_dibujado": None,
    "union_de_zonas": None,
}
RISK_BADGE = {
    "3/3": ("#ef4444", "RIESGO MÁXIMO"),
    "2/3": ("#f97316", "RIESGO ALTO"),
    "1/3": ("#3b82f6", "VIGILANCIA"),
}
ORDEN_RIESGO = {"3/3": 0, "2/3": 1, "1/3": 2}
# desempate por urgencia temporal dentro de cada nivel de riesgo: con datos frescos (FIRMS API
# directa, <3h de latencia - ver src/firms_api.py) ya se puede distinguir de verdad un foco
# "Activo" ahora mismo de uno "Controlado" (ver umbrales en src/risk.py), asi que dentro del
# mismo nivel de riesgo interesa ver antes el que sigue activo, no solo el mas cercano
ORDEN_URGENCIA = {"Activo": 0, "En seguimiento": 1, "Controlado": 2}


def _orden_urgencia(ultima_deteccion) -> int:
    _, _, estado_texto = estado_foco(ultima_deteccion)
    return ORDEN_URGENCIA.get(estado_texto, 3)

# ============================== ESTILO ==============================

MAP_HEIGHT = 640

st.markdown(f"""
<style>
    #MainMenu, footer, header[data-testid="stHeader"] {{visibility: hidden; height: 0;}}
    .block-container {{padding-top: 0.6rem; padding-bottom: 0.6rem; max-width: 1600px;}}
    section[data-testid="stSidebar"] {{display: none;}}

    .ix-hero {{
        background: linear-gradient(120deg, #7c1d0f 0%, #b8380f 45%, #d9631a 100%);
        border-radius: 12px;
        padding: 12px 20px;
        color: #fff;
        box-shadow: 0 6px 18px rgba(184,56,15,0.25);
        display: flex;
        align-items: baseline;
        gap: 14px;
        flex-wrap: wrap;
    }}
    .ix-hero h1 {{margin: 0; font-size: 1.3rem; font-weight: 700; letter-spacing: -0.01em; white-space: nowrap;}}
    .ix-hero p {{margin: 0; opacity: 0.9; font-size: 0.82rem;}}

    .ix-toolbar {{
        background-color: rgba(127,127,127,0.06);
        border: 1px solid rgba(127,127,127,0.15);
        border-radius: 10px;
        padding: 8px 16px 0px 16px;
        margin: 8px 0 8px 0;
    }}
    .ix-toolbar div[data-testid="stTextInput"] label,
    .ix-toolbar div[data-testid="stMultiSelect"] label,
    .ix-toolbar div[data-testid="stSlider"] label {{font-size: 0.72rem;}}

    div[data-testid="stMetric"] {{
        background-color: rgba(127,127,127,0.06);
        border: 1px solid rgba(127,127,127,0.12);
        border-radius: 8px;
        padding: 6px 10px;
    }}
    div[data-testid="stMetric"] label {{font-size: 0.68rem; opacity: 0.75;}}
    div[data-testid="stMetricValue"] {{font-size: 1.1rem;}}

    .ix-section-title {{
        font-size: 0.92rem;
        font-weight: 700;
        margin: 2px 0 6px 0;
        display: flex;
        align-items: center;
        gap: 6px;
    }}

    .ix-badge {{
        display: inline-block;
        padding: 2px 9px;
        border-radius: 999px;
        font-size: 0.68rem;
        font-weight: 700;
        color: #fff;
        letter-spacing: 0.02em;
    }}

    div[data-testid="stExpander"] {{
        border: 1px solid rgba(127,127,127,0.15);
        border-radius: 8px;
    }}
    div[data-testid="stExpander"] summary {{font-size: 0.85rem; padding: 6px 10px;}}

    .ix-legend {{opacity: 0.7; font-size: 0.74rem; margin-bottom: 4px;}}

    /* paneles con scroll interno, para que la pagina entera no haga scroll */
    div[data-testid="stVerticalBlockBorderWrapper"] > div > div[data-testid="stVerticalBlock"] {{
        gap: 0.4rem;
    }}

    /* mapa de España (columna izquierda) fijo en pantalla al hacer scroll - el panel derecho
    (ranking/lista, que puede crecer mucho con los avisos desplegados) hace scroll normal de
    pagina, pero el mapa se queda anclado en vista en vez de desaparecer hacia arriba. El ancla
    invisible .ix-map-sticky-anchor (primer elemento dentro de la columna del mapa) permite
    seleccionar SOLO esa columna con :has(), sin afectar a las demas columnas de la pagina */
    div[data-testid="stColumn"]:has(> div .ix-map-sticky-anchor) {{
        position: sticky;
        top: 0.6rem;
        align-self: flex-start;
    }}
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=1800, show_spinner="Cargando ranchos de clientes desde BD...")
def _cargar_ranchos():
    return obtener_ranchos_es()


@st.cache_data(ttl=1800, show_spinner="Cargando zonas de clientes desde BD...")
def _cargar_zonas():
    return obtener_zonas_es()


@st.cache_data(ttl=900, show_spinner="Consultando hotspots en Google Earth Engine...")
def _cargar_hotspots_gee(bbox, window_hours):
    return obtener_hotspots_gee(bbox, window_hours), datetime.now(timezone.utc)


@st.cache_data(ttl=900, show_spinner="Consultando hotspots (FIRMS API directa)...")
def _cargar_hotspots_firms(bbox, dias):
    return obtener_hotspots_firms_api(bbox, dias=dias), datetime.now(timezone.utc)


def _cargar_hotspots(bbox, window_hours):
    """Preferencia FIRMS API directa (latencia <3h documentada por NASA, ver investigacion de
    2026-07-27) sobre el espejo de Earth Engine (latencia observada de 24-40h - GEE no reingesta
    las colecciones NRT de NASA LANCE con la misma frecuencia que la fuente original). Si no hay
    FIRMS_MAP_KEY configurada, o la consulta a FIRMS falla por cualquier motivo (limite de
    transacciones, MAP_KEY invalida, caida del servicio...), se cae a GEE sin romper la carga de
    la app - misma logica de "degradar, no romper" que ya se usa en el resto del pipeline."""
    if os.getenv("FIRMS_MAP_KEY"):
        dias = min(max(round(window_hours / 24), 1), 5)  # la Area API de FIRMS admite 1-5 dias
        try:
            hotspots, fetched_at = _cargar_hotspots_firms(bbox, dias)
            return hotspots, fetched_at, "FIRMS API directa"
        except Exception:
            pass
    hotspots, fetched_at = _cargar_hotspots_gee(bbox, window_hours)
    return hotspots, fetched_at, "Google Earth Engine"


@st.cache_data(ttl=900, show_spinner="Agrupando y geolocalizando incendios (Nominatim)...")
def _procesar_incendios(hotspots):
    hotspots_enriquecido, incendios = identificar_incendios(hotspots)
    incendios = geocodificar_incendios(incendios)
    # fuera de interes: incendios reales de paises vecinos (Marruecos, Argelia, Portugal...)
    # captados por el margen +20km del bbox de consulta
    hotspots_enriquecido, incendios = excluir_paises(hotspots_enriquecido, incendios)

    avance = calcular_avance(hotspots_enriquecido)
    incendios = incendios.merge(avance, on="fire_id", how="left")

    hotspots_enriquecido = hotspots_enriquecido.merge(
        incendios[["fire_id", "localidad", "municipio", "provincia", "primera_deteccion",
                   "ultima_deteccion", "duracion_horas", "n_detecciones",
                   "direccion_avance_es", "velocidad_kmh", "avance_confiable"]],
        on="fire_id", how="left",
    )
    return hotspots_enriquecido, incendios


def _bbox_de_ranchos(ranchos) -> tuple[float, float, float, float]:
    b = ranchos.total_bounds  # (minx, miny, maxx, maxy)
    margen = 0.2  # grados, ~20 km, para no perder hotspots justo en el borde
    return (b[0] - margen, b[1] - margen, b[2] + margen, b[3] + margen)


def _filtrar_ranchos(df, texto, regiones):
    if texto:
        t = texto.strip().lower()
        df = df[df["ranch_name"].str.lower().str.contains(t, na=False)
                | df["customer_name"].str.lower().str.contains(t, na=False)]
    if regiones:
        df = df[df["region"].isin(regiones)]
    return df


def _badge_html(risk_level: str) -> str:
    color, label = RISK_BADGE.get(risk_level, ("#6b7280", risk_level))
    return f'<span class="ix-badge" style="background-color:{color}">{label} · {risk_level}</span>'


_estado_foco = estado_foco  # alias local, ver import de src.risk mas arriba


def _estado_foco_badge_html(ultima_deteccion) -> str:
    label, color, _ = _estado_foco(ultima_deteccion)
    if not label:
        return ""
    return f'<span class="ix-badge" style="background-color:{color}">{label}</span>'


def _color_por_antiguedad(acq_datetime) -> tuple[str, str]:
    """Color del hotspot segun horas transcurridas desde su deteccion (label, color hex) -
    morado = recien detectado, va virando a rojo/naranja/amarillo/gris cuanto mas antiguo."""
    horas = (pd.Timestamp.now(tz="UTC") - acq_datetime).total_seconds() / 3600.0
    for i, umbral in enumerate(HOTSPOT_AGE_BINS_H):
        if horas <= umbral:
            label = f"≤{umbral}h" if i == 0 else f"{HOTSPOT_AGE_BINS_H[i - 1]}-{umbral}h"
            return label, HOTSPOT_AGE_COLORS[label]
    label = f">{HOTSPOT_AGE_BINS_H[-1]}h"
    return label, HOTSPOT_AGE_COLORS[label]


def _tooltip_zona(row, incluir_tipo: bool = False, zona_individual: str | None = None) -> str:
    # HTML: GeoJsonTooltip inserta el valor de cada campo via innerHTML, asi que las etiquetas
    # <b>/<i> se renderizan - nombre de la ganaderia en negrita + zona(s), para identificar la
    # finca (y, si se pasa zona_individual, la parcela concreta bajo el cursor) al hacer hover
    ranch_name = row["ranch_name"]
    customer_name = row["customer_name"]
    lineas = [f"<b>{ranch_name}</b>"]
    # autonomos: el nombre del cliente y el de la ganaderia suelen coincidir - mostrarlo una
    # sola vez en vez de repetido en dos lineas
    if customer_name and customer_name != ranch_name:
        lineas.append(customer_name)
    telefono = row.get("customer_phone")
    if pd.notna(telefono) and telefono:
        lineas.append(f"📞 {telefono}")
    if incluir_tipo and row.get("tipo_ganaderia"):
        lineas.append(row["tipo_ganaderia"])
    if zona_individual:
        # se esta dibujando UNA parcela concreta (rancho con varias zonas dispersas fusionadas
        # para el calculo de distancia) - mostrar solo el nombre de ESA parcela, no la lista
        # entera, que es lo que impedia saber cual era cual al pasar el cursor
        lineas.append(f"<i>Zona: {zona_individual}</i>")
    else:
        zonas = row.get("zonas_nombres") or ""
        if zonas:
            lineas.append(f"<i>Zonas: {zonas}</i>")
    return "<br>".join(lineas)


def _mapa_principal(ranchos_df, hotspots_df, resaltar_ids=None, centro=None, zoom=6, zonas_df=None):
    resaltar_ids = resaltar_ids or set()
    m = folium.Map(location=centro, zoom_start=zoom, tiles="CartoDB positron")
    folium.TileLayer("Esri.WorldImagery", name="Satélite").add_to(m)

    # zonas individuales (sin fusionar), agrupadas por rancho, para dibujar cada parcela por
    # separado con su propio nombre en vez de la geometria fusionada de "union_de_zonas" - sin
    # esto (zonas_df is None: sin BD, o snapshot generado antes de incluir esta capa) se cae al
    # comportamiento anterior, una sola forma fusionada por rancho con tooltip generico
    zonas_por_rancho = (
        {rid: grupo for rid, grupo in zonas_df.groupby("ranch_id")} if zonas_df is not None else {}
    )

    # una sola capa GeoJson (FeatureCollection) para todos los ranchos/zonas, en vez de un
    # folium.GeoJson por fila - Leaflet monta 1 capa vectorial en vez de N, mucho mas rapido
    # de renderizar en el navegador cuando hay cientos de ranchos
    features = []
    for _, row in ranchos_df.iterrows():
        resaltado = row["ranch_id"] in resaltar_ids
        color = COLOR_FUENTE[row["fuente_geometria"]]
        dash = DASH_FUENTE[row["fuente_geometria"]]
        style_props = {
            "color": "#ffd166" if resaltado else color,
            "weight": 4 if resaltado else 1.5,
            "fillOpacity": 0.35 if resaltado else 0.12,
            "dashArray": dash if (dash and not resaltado) else "",
        }
        zonas_rancho = (
            zonas_por_rancho.get(row["ranch_id"]) if row["fuente_geometria"] == "union_de_zonas" else None
        )
        if zonas_rancho is not None and not zonas_rancho.empty:
            for _, zona in zonas_rancho.iterrows():
                features.append({
                    "type": "Feature",
                    "geometry": zona["geometry"].__geo_interface__,
                    "properties": {
                        **style_props,
                        "tooltip": _tooltip_zona(row, incluir_tipo=True, zona_individual=zona.get("zone_name") or "Sin nombre"),
                    },
                })
        else:
            features.append({
                "type": "Feature",
                "geometry": row["geometry"].__geo_interface__,
                "properties": {**style_props, "tooltip": _tooltip_zona(row, incluir_tipo=True)},
            })
    if features:
        def _style_rancho(feature):
            props = feature["properties"]
            style = {"color": props["color"], "weight": props["weight"], "fillOpacity": props["fillOpacity"]}
            if props["dashArray"]:
                style["dashArray"] = props["dashArray"]
            return style

        folium.GeoJson(
            {"type": "FeatureCollection", "features": features},
            style_function=_style_rancho,
            # al pasar el cursor sobre una zona, se resalta con borde grueso y color destacado
            # para saber de un vistazo cual es, independientemente de su estilo base
            highlight_function=lambda _f: {"weight": 5, "color": "#ffd166", "fillOpacity": 0.5},
            tooltip=folium.GeoJsonTooltip(fields=["tooltip"], aliases=[""], labels=False, sticky=True),
        ).add_to(m)

    if hotspots_df is not None and not hotspots_df.empty:
        # sin clustering (se quieren ver todos los puntos individuales, no agrupados en un
        # icono con contador) pero consolidados en UNA sola capa GeoJson en vez de un
        # folium.CircleMarker por fila - con miles de hotspots, montar miles de capas Leaflet
        # por separado es lo que hacia lenta la carga del mapa; una FeatureCollection con
        # marker=CircleMarker(...) da el mismo resultado visual mucho mas rapido
        features_hs = []
        for _, hs in hotspots_df.iterrows():
            lugar = ", ".join(p for p in [hs.get("localidad"), hs.get("provincia")] if p)
            edad_label, edad_color = _color_por_antiguedad(hs["acq_datetime"])
            features_hs.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [hs["longitude"], hs["latitude"]]},
                "properties": {
                    "color": edad_color,
                    "tooltip": f"Hotspot {hs['source']} · {edad_label}" + (f" · {lugar}" if lugar else "")
                               + f" · {hs['acq_datetime']:%Y-%m-%d %H:%M} UTC",
                },
            })

        def _style_hotspot(feature):
            color = feature["properties"]["color"]
            return {"color": color, "fillColor": color, "fillOpacity": 0.85, "weight": 1}

        folium.GeoJson(
            {"type": "FeatureCollection", "features": features_hs},
            name="Hotspots",
            marker=folium.CircleMarker(radius=5, fill=True),
            style_function=_style_hotspot,
            tooltip=folium.GeoJsonTooltip(fields=["tooltip"], aliases=[""], labels=False),
        ).add_to(m)

    # herramienta de regla (distancias/areas) en el mapa principal, para medir a ojo la
    # separacion entre un foco y una finca sin depender solo de los anillos de riesgo
    MeasureControl(
        position="topleft", primary_length_unit="kilometers", secondary_length_unit="meters",
        primary_area_unit="hectares",
    ).add_to(m)

    folium.LayerControl().add_to(m)
    return m


_anillos_riesgo = anillos_riesgo  # alias local, ver import de src.risk mas arriba


def _hotspots_cercanos_de(rancho_row, hotspots_df):
    # ojo: filtrar por Ranches.Location (rancho_row["lat"]/["lon"]) es un bug si la geometria
    # real (union de varias Zones, a veces dispersas) queda lejos de ese punto guardado - se usa
    # en su lugar el propio bbox del anillo de 10km (el mismo que se dibuja), que por
    # construccion siempre contiene cualquier hotspot a <=10km del poligono
    minx, miny, maxx, maxy = _anillos_riesgo(rancho_row["geometry"])[-1][1].bounds
    return hotspots_df[
        hotspots_df["longitude"].between(minx, maxx) & hotspots_df["latitude"].between(miny, maxy)
    ]


def _hotspots_mismo_fuego_de(aviso_row, hotspots_df):
    if pd.isna(aviso_row.get("fire_id")):
        return None
    return hotspots_df[hotspots_df["fire_id"] == aviso_row["fire_id"]]


def _hotspots_vista_general_de(rancho_row, hotspots_df):
    # mismo criterio que _hotspots_cercanos_de pero con el bbox mas ancho del mapa de vista
    # general del informe PDF (rectangulo alargado) - sin esto, ese mapa mostraria terreno sin
    # ningun hotspot en los bordes laterales aunque hubiera focos activos ahi
    minx, miny, maxx, maxy = bbox_vista_general(rancho_row["geometry"])
    return hotspots_df[
        hotspots_df["longitude"].between(minx, maxx) & hotspots_df["latitude"].between(miny, maxy)
    ]


def _mapa_mini_aviso(rancho_row, aviso_row, hotspots_cercanos, posiciones_animales=None):
    """Mapa individual de un aviso: perimetro del rancho, anillos de riesgo (zonas de seguridad),
    hotspot(s) cercanos, linea de distancia entre el centroide del rancho y el hotspot que
    disparo el aviso, y (si hay BD en vivo) la ultima posicion conocida de los animales del
    rancho - para ver de un vistazo si el ganado esta cerca del foco, no solo la finca."""
    centroid = rancho_row["geometry"].centroid
    m = folium.Map(location=[centroid.y, centroid.x], zoom_start=11, tiles="Esri.WorldImagery")

    # anillos de mayor a menor radio, para que el mas pequeño quede encima visualmente
    for label, anillo_geom in reversed(_anillos_riesgo(rancho_row["geometry"])):
        folium.GeoJson(
            anillo_geom.__geo_interface__,
            style_function=lambda _f, color=COLORS_RING[label]: {
                "color": color, "weight": 1.5, "fillColor": color, "fillOpacity": 0.08, "dashArray": "4,4",
            },
            tooltip=f"Zona {label} · riesgo {RING_RISK[label]}",
        ).add_to(m)

    folium.GeoJson(
        rancho_row["geometry"].__geo_interface__,
        style_function=lambda _f: {"color": "#ffd166", "weight": 3, "fillOpacity": 0.15},
        tooltip=_tooltip_zona(rancho_row),
    ).add_to(m)

    for _, hs in hotspots_cercanos.iterrows():
        edad_label, edad_color = _color_por_antiguedad(hs["acq_datetime"])
        folium.CircleMarker(
            location=[hs["latitude"], hs["longitude"]],
            radius=6, color=edad_color, fill=True, fill_color=edad_color, fill_opacity=0.9,
            tooltip=f"{hs['source']} · {edad_label} · {hs['acq_datetime']:%Y-%m-%d %H:%M} UTC",
        ).add_to(m)

    folium.PolyLine(
        locations=[[centroid.y, centroid.x], [aviso_row["hotspot_lat"], aviso_row["hotspot_lon"]]],
        color="#ffd166", weight=2, dash_array="6,6",
        tooltip=f"{aviso_row['distance_km']:.1f} km",
    ).add_to(m)

    if posiciones_animales is not None and not posiciones_animales.empty:
        ahora = pd.Timestamp.now(tz="UTC")
        for _, pos in posiciones_animales.iterrows():
            hace_h = (ahora - pos["ultima_posicion"]).total_seconds() / 3600.0
            folium.CircleMarker(
                location=[pos["lat"], pos["lon"]],
                radius=4, color="#38bdf8", fill=True, fill_color="#38bdf8", fill_opacity=0.9,
                tooltip=f"🐾 {pos.get('specie_es') or 'Animal'} · hace {formatear_duracion(hace_h)}",
            ).add_to(m)

    MeasureControl(
        position="topleft", primary_length_unit="kilometers", secondary_length_unit="meters",
        primary_area_unit="hectares",
    ).add_to(m)

    # encuadre que cubra la mayor zona de seguridad (10 km) ademas del hotspot, para que se
    # vean los anillos completos y no solo el segmento centroide-hotspot
    minx, miny, maxx, maxy = _anillos_riesgo(rancho_row["geometry"])[-1][1].bounds
    bounds = [[miny, minx], [maxy, maxx], [aviso_row["hotspot_lat"], aviso_row["hotspot_lon"]]]
    m.fit_bounds(bounds, padding=(20, 20))
    return m


# ============================== ESQUELETO DE LA PAGINA (orden visual) ==============================
# todo cabe en una sola pantalla: cabecera y filtros compactos arriba, debajo KPIs en una fila,
# y el bloque principal en dos columnas (mapa grande | panel con pestañas) de la misma altura,
# con scroll interno en el panel en vez de scroll de pagina.

ph_header = st.container()
ph_toolbar = st.container()
ph_kpis = st.container()
col_mapa, col_panel = st.columns([1, 1.15])

# ============================== CABECERA ==============================

with ph_header:
    col_titulo, col_info = st.columns([6, 1])
    with col_titulo:
        st.markdown("""
        <div class="ix-hero">
            <h1>🔥 Panel de riesgo de incendio</h1>
            <p>Ixorigue — cruce automático de ranchos de clientes ES contra hotspots de incendio en tiempo casi real</p>
        </div>
        """, unsafe_allow_html=True)
    with col_info:
        with st.popover("ℹ️ Cómo funciona", width="stretch"):
            st.markdown(f"""
**Ranchos y su geometría** — solo se incluyen ranchos con geometría real dibujada por el cliente,
en este orden de preferencia:
1. 🔵 **Perímetro real**: el límite que el cliente dibujó en la app (`Zones` marcada como perímetro).
2. 🟢 **Unión de zonas**: si no hay perímetro, se unen todas las parcelas de pasto activas del rancho.

Los ranchos sin ninguna zona dibujada (solo un punto de ubicación) no se muestran en este panel.

**Detección de incendios** — se consultan puntos calientes (hotspots) de los satélites VIIRS
(NOAA-20/SNPP) y MODIS, dentro de una ventana fija de {WINDOW_HOURS_DEFAULT}h hacia atrás
(ampliarla más solo añade hotspots ya extinguidos, ver aviso más abajo). Fuente de datos: la
**Area API directa de NASA FIRMS** (latencia &lt;3h) siempre que haya una `FIRMS_MAP_KEY`
configurada; si falta o falla, se cae automáticamente a un espejo de las mismas colecciones en
**Google Earth Engine** (latencia observada de 24-40h — mucho más lento, se usa solo como
respaldo). El caption bajo los KPIs indica de cuál de las dos viene el dato mostrado.
Los hotspots cercanos en espacio y tiempo se agrupan en "incendios" para no repetir el mismo
fuego muchas veces. Se descartan los incendios geocodificados fuera de España (el margen de
+20 km del área de consulta capta a veces incendios reales de Marruecos, Argelia, Portugal o
Francia).

Cada punto del mapa se colorea según su antigüedad (morado = recién detectado, virando a rojo,
naranja, amarillo, verde oliva y finalmente gris — probablemente ya extinguido — cuanto más
antigua es la detección dentro de la ventana consultada).

**⚠️ No es monitorización continua** — estos satélites son de órbita polar y solo sobrevuelan cada
punto de España unas pocas veces al día:
- VIIRS NOAA-20 y VIIRS SNPP: ~2 pasadas/día cada uno (una diurna, una nocturna).
- MODIS (Terra/Aqua): pasadas adicionales, ~2/día combinadas.

En total, un rancho puede recibir como mucho **6-8 actualizaciones de datos al día**, repartidas de
forma irregular — pero con FIRMS API directa, cada una de esas pasadas llega a la app en menos de
3h desde que el satélite la capta (con el respaldo de Earth Engine, ese mismo dato puede tardar
24-40h en aparecer). Además, como los hotspots pueden proceder de varias fuentes, un mismo foco
real puede aparecer duplicado con `source` distinto.

**Nivel de riesgo por rancho** — para cada hotspot se calcula la distancia al perímetro del rancho
más cercano y se asigna un anillo:
- **Dentro del rancho o a ≤3 km** → riesgo `{RING_RISK["0-3 km"]}` (máximo)
- **3–5 km** → riesgo `{RING_RISK["3-5 km"]}` (alto)
- **5–10 km** → riesgo `{RING_RISK["5-10 km"]}` (vigilancia)
- **Más de 10 km** → sin aviso

En el mapa de cada aviso (pestaña "🏆 Ranking de riesgo") se dibujan estos tres anillos como zonas
de seguridad alrededor del perímetro real del rancho, para visualizar de un vistazo cuánto margen
queda hasta el siguiente nivel de riesgo.

**Tendencia de avance del incendio** — cuando hay suficientes detecciones repartidas en el tiempo
(≥3 hotspots, ≥1h de ventana, con movimiento neto por encima del ruido de localización del sensor),
se estima una dirección y velocidad aproximadas comparando el centroide de las primeras detecciones
con el de las últimas. Es una aproximación orientativa a partir de datos satelitales, **no** un
modelo real de propagación de incendios (no tiene en cuenta viento, combustible ni pendiente).

Un rancho solo genera un aviso por el hotspot más grave. La **dirección** es el rumbo
(N/NE/E/SE/S/SO/O/NO) desde el centroide del rancho hacia el hotspot (bearing great-circle).
""")

# ============================== BARRA DE FILTROS (se calcula antes del mapa) ==============================

ranchos = _cargar_ranchos()
zonas = _cargar_zonas()
bbox = _bbox_de_ranchos(ranchos)

OPCION_SIN_GANADERIA = "(todas las ganaderías)"

with ph_toolbar:
    st.markdown('<div class="ix-toolbar">', unsafe_allow_html=True)
    c_busq, c_region, c_niveles, c_recargar = st.columns([2, 2, 1.8, 1])
    with c_busq:
        opciones_ganaderia = [OPCION_SIN_GANADERIA] + sorted(ranchos["ranch_name"].dropna().unique())
        ganaderia_sel = st.selectbox("Buscar ganadería", options=opciones_ganaderia, index=0)
        busqueda = "" if ganaderia_sel == OPCION_SIN_GANADERIA else ganaderia_sel
    with c_region:
        regiones_sel = st.multiselect("Comunidad autónoma", options=sorted(ranchos["region"].dropna().unique()))
    with c_niveles:
        niveles = st.multiselect("Nivel de riesgo", options=["3/3", "2/3", "1/3"], default=["3/3", "2/3", "1/3"])
    with c_recargar:
        st.write("")
        if st.button("↻ Recargar hotspots", width="stretch"):
            _cargar_hotspots_firms.clear()
            _cargar_hotspots_gee.clear()
            _procesar_incendios.clear()
    st.markdown('</div>', unsafe_allow_html=True)

window_hours = WINDOW_HOURS_DEFAULT  # fijo: cuanto mas atras se mire, mas hotspots "fantasma" ya extinguidos aparecen
ranchos_filtrados = _filtrar_ranchos(ranchos, busqueda, regiones_sel)
hay_filtro = bool(busqueda or regiones_sel)

# ============================== CARGA DE DATOS (incendios/avisos) ==============================

try:
    hotspots_crudos, fetched_at, fuente_hotspots = _cargar_hotspots(bbox, window_hours)
    hotspots, incendios = _procesar_incendios(hotspots_crudos)
    error_gee = None
except Exception as e:
    hotspots, incendios, fetched_at, fuente_hotspots = None, None, None, None
    error_gee = str(e)

avisos = evaluar_riesgo(ranchos, hotspots) if hotspots is not None and not hotspots.empty else None
avisos_filtrados = avisos
if avisos is not None:
    ids_filtrados = set(ranchos_filtrados["ranch_id"])
    avisos_filtrados = avisos[avisos["ranch_id"].isin(ids_filtrados)]
    avisos_filtrados = avisos_filtrados[avisos_filtrados["risk_level"].isin(niveles)]

# ============================== MAPA (columna izquierda, mitad de la ventana) ==============================

if "foco_ranch_id" not in st.session_state:
    st.session_state["foco_ranch_id"] = None

with col_mapa:
    st.markdown('<div class="ix-map-sticky-anchor"></div>', unsafe_allow_html=True)
    foco_id = st.session_state["foco_ranch_id"]

    col_titulo_mapa, col_quitar_foco = st.columns([5, 1.3])
    with col_titulo_mapa:
        subtitulo = f"🗺️ Mapa de ranchos — {len(ranchos_filtrados)} / {len(ranchos)}"
        if hay_filtro:
            subtitulo += "  ·  filtrado"
        if foco_id is not None:
            subtitulo += "  ·  🎯 zona seleccionada"
        st.markdown(f'<div class="ix-section-title">{subtitulo}</div>', unsafe_allow_html=True)
    with col_quitar_foco:
        if foco_id is not None and st.button("✖ Quitar foco", width="stretch"):
            st.session_state["foco_ranch_id"] = None
            st.rerun()

    leyenda_hotspots = " &nbsp;·&nbsp; ".join(
        f'<span style="color:{color}">●</span> {label}' for label, color in HOTSPOT_AGE_COLORS.items()
    )
    st.markdown(
        '<p class="ix-legend">🔵 Perímetro real &nbsp;·&nbsp; 🟢 Unión de zonas &nbsp;·&nbsp; '
        '🟡 Coincide con el filtro/foco &nbsp;·&nbsp; '
        f'Hotspots por antigüedad: {leyenda_hotspots}</p>',
        unsafe_allow_html=True,
    )
    if error_gee:
        st.error(f"No se pudieron consultar hotspots (ni FIRMS API ni Google Earth Engine): {error_gee}")

    if foco_id is not None and foco_id in set(ranchos["ranch_id"]):
        rancho_foco = ranchos.loc[ranchos["ranch_id"] == foco_id].iloc[0]
        centro = [rancho_foco["lat"], rancho_foco["lon"]]
        resaltar = {foco_id}
        m = _mapa_principal(ranchos, hotspots, resaltar_ids=resaltar, centro=centro, zoom=13, zonas_df=zonas)
        # encuadre a la extension REAL de la geometria del rancho (no solo su centro con un zoom
        # fijo) - una finca grande o alargada se ve completa, no solo recortada por el zoom 13
        minx, miny, maxx, maxy = rancho_foco["geometry"].bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]], padding=(40, 40))
        st_folium(m, width=None, height=MAP_HEIGHT, returned_objects=[], key="mapa_general")
    elif ranchos_filtrados.empty:
        st.warning("Ningún rancho coincide con los filtros.")
    else:
        centro = [ranchos_filtrados["lat"].mean(), ranchos_filtrados["lon"].mean()]
        zoom = 6 if not hay_filtro else 9
        resaltar = set(ranchos_filtrados["ranch_id"]) if hay_filtro else None
        m = _mapa_principal(ranchos, hotspots, resaltar_ids=resaltar, centro=centro, zoom=zoom, zonas_df=zonas)
        if hay_filtro:
            # encuadre exacto a la(s) zona(s) que coinciden con el filtro (busqueda, tipo o
            # region), esten o no en el ranking de riesgo - el zoom fijo de arriba es solo
            # el punto de partida antes de ajustar a los limites reales de la geometria
            minx, miny, maxx, maxy = ranchos_filtrados.total_bounds
            m.fit_bounds([[miny, minx], [maxy, maxx]], padding=(30, 30))
        st_folium(m, width=None, height=MAP_HEIGHT, returned_objects=[], key="mapa_general")

# ============================== KPIs ==============================

n_clientes = ranchos["customer_name"].nunique()
n_riesgo_max = int((avisos["risk_level"] == "3/3").sum()) if avisos is not None else 0
n_riesgo_alto = int((avisos["risk_level"] == "2/3").sum()) if avisos is not None else 0
n_riesgo_vig = int((avisos["risk_level"] == "1/3").sum()) if avisos is not None else 0
n_incendios = len(incendios) if incendios is not None else 0
n_perimetro_real = int((ranchos["fuente_geometria"] == "perimetro_dibujado").sum())

with ph_kpis:
    if RANCHOS_DATA_SOURCE == "snapshot" and "snapshot_exportado_en" in ranchos.columns:
        exportado_en = pd.to_datetime(ranchos["snapshot_exportado_en"].iloc[0]).tz_convert("Europe/Madrid")
        st.caption(f"🗂️ Ranchos/clientes: snapshot local del {exportado_en:%Y-%m-%d %H:%M} · los datos de incendio siguen en tiempo real.")
    if fetched_at:
        edad_min = int((datetime.now(timezone.utc) - fetched_at).total_seconds() / 60)
        st.caption(f"🛰️ Última consulta ({fuente_hotspots}): hace {edad_min} min · ventana de {window_hours}h · {n_clientes} clientes activos, {len(ranchos)} ranchos activos.")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(
        "Clientes activos", n_clientes,
        help="Clientes con al menos un rancho con zonas y/o perímetro dibujados en la app - los ranchos sin ninguna zona dibujada (solo un punto de ubicación) no se cuentan aquí.",
    )
    k2.metric(
        "Ranchos activos", len(ranchos),
        help="Ranchos con zonas y/o perímetro dibujados en la app - se excluyen los ranchos sin ninguna zona dibujada (solo un punto de ubicación).",
    )
    k3.metric("🔵 Con perímetro dibujado", f"{n_perimetro_real} / {len(ranchos)}", help="Ranchos con perímetro real dibujado por el cliente, frente al total (el resto usa unión de zonas de pasto)")
    k4.metric("🔥 Incendios en España", n_incendios)

    k5, k6, k7, k8 = st.columns(4)
    k5.metric("Hotspots", len(hotspots) if hotspots is not None else 0)
    k6.metric("🔴 Riesgo máximo", n_riesgo_max)
    k7.metric("🟠 Riesgo alto", n_riesgo_alto)
    k8.metric("🔵 Vigilancia", n_riesgo_vig)

# ============================== PANEL DERECHO CON PESTAÑAS (misma altura que el mapa) ==============================

PANEL_HEIGHT = MAP_HEIGHT - 42  # descuenta la fila de pestañas

with col_panel:
    tab_ranking, tab_lista = st.tabs(["🏆 Ranking de riesgo", "📋 Lista de ganaderías"])

    # ---- RANKING DE GANADERIAS EN RIESGO ----
    with tab_ranking:
        # sin altura fija: al desplegar un aviso (con su mini-mapa, mas grande que antes) se
        # quiere ver toda la info sin que quede recortada/diminuta dentro de una caja con
        # scroll interno del tamaño del mapa principal - esta pestaña crece con su contenido
        # y la pagina hace scroll normal (la pestaña "Lista" si mantiene altura fija, es una
        # tabla y tiene sentido que se comporte como tal)
        with st.container(border=False):
            if error_gee:
                st.info("Sin datos de GEE — no se puede calcular el ranking.")
            elif hotspots is None or hotspots.empty:
                st.success(f"Sin hotspots en los últimos {window_hours}h.")
            elif avisos is None or avisos.empty:
                st.success(f"{len(hotspots)} hotspots detectados, ninguno a menos de 10 km de un rancho.")
            elif avisos_filtrados.empty:
                st.info("Ningún aviso coincide con los filtros actuales.")
            else:
                ranking = avisos_filtrados.copy()
                ranking["_orden"] = ranking["risk_level"].map(ORDEN_RIESGO)
                ranking["_urgencia"] = ranking["ultima_deteccion"].apply(_orden_urgencia)
                ranking = ranking.sort_values(["_orden", "_urgencia", "distance_km"]).reset_index(drop=True)
                st.caption(f"{len(ranking)} / {len(avisos)} ganaderías con foco a menos de 10 km, ordenadas de más a menos grave (y, dentro de cada nivel, focos activos antes que controlados).")

                for i, aviso in ranking.iterrows():
                    telefono = aviso.get("customer_phone")
                    _, _, estado_corto = _estado_foco(aviso.get("ultima_deteccion"))
                    estado_sufijo = f" · {estado_corto}" if estado_corto else ""
                    etiqueta = (
                        f"#{i + 1} · {aviso['risk_level']}{estado_sufijo} · "
                        f"{aviso['ranch_name']} — {aviso['customer_name']}"
                    )
                    titulo = (
                        f'{_badge_html(aviso["risk_level"])} '
                        f'{_estado_foco_badge_html(aviso.get("ultima_deteccion"))} '
                        f'&nbsp; <b>{aviso["ranch_name"]}</b> '
                        f'&nbsp;·&nbsp; {aviso["customer_name"]}'
                        + (f" &nbsp;·&nbsp; 📞 {telefono}" if pd.notna(telefono) and telefono else "")
                    )
                    with st.expander(etiqueta, expanded=(i == 0)):
                        col_titulo_aviso, col_boton_foco = st.columns([4, 1.4])
                        with col_titulo_aviso:
                            st.markdown(titulo, unsafe_allow_html=True)
                        with col_boton_foco:
                            if st.button(
                                "🔎 Centrar mapa general", key=f"foco_btn_{aviso['ranch_id']}", width="stretch",
                                help="Centra y hace zoom en el MAPA GRANDE de la izquierda sobre esta finca (no genera nada nuevo, solo mueve la vista).",
                            ):
                                st.session_state["foco_ranch_id"] = aviso["ranch_id"]
                                st.rerun()
                        st.write("")
                        c1, c2, c3, c4, c5 = st.columns(5)
                        c1.metric("Distancia", f"{aviso['distance_km']:.1f} km")
                        c2.metric("Anillo", aviso["ring"])
                        c3.metric("Dirección", aviso["direction_es"].capitalize())
                        duracion_h = aviso.get("duracion_horas")
                        c4.metric(
                            "Duración detectado", formatear_duracion(duracion_h) if pd.notna(duracion_h) else "—",
                            help="Tiempo entre la primera y la última detección satelital de este foco - no indica si sigue activo ahora mismo (para eso, ver el badge de estado junto al riesgo).",
                        )
                        ultima_det = aviso.get("ultima_deteccion")
                        c5.metric(
                            "Última act.",
                            ultima_det.tz_convert("Europe/Madrid").strftime("%d/%m %H:%M") if pd.notna(ultima_det) else "—",
                            help="Hora local de la detección satelital más reciente de este incendio.",
                        )

                        if aviso.get("avance_confiable"):
                            st.markdown(
                                f"🧭 **Tendencia de avance estimada:** hacia el {aviso['direccion_avance_es']} "
                                f"&nbsp;·&nbsp; ~{aviso['velocidad_kmh']:.1f} km/h "
                                f"(orientativa, a partir de las detecciones satelitales — no un modelo de propagación real)"
                            )
                        else:
                            st.caption("🧭 Datos insuficientes para estimar la tendencia de avance de este incendio.")

                        st.markdown(
                            f"🗂️ **Zona:** {aviso.get('zona_nombre') or '—'} &nbsp;·&nbsp; "
                            f"🐄 **Tipo de ganadería:** {aviso.get('tipo_ganaderia') or '—'} &nbsp;·&nbsp; "
                            f"🗺️ **Región:** {aviso.get('region') or '—'} &nbsp;·&nbsp; "
                            f"📍 **Localización del foco:** {aviso.get('lugar') or 'sin resolver'}"
                        )
                        st.info(aviso["mensaje_final"])
                        st.caption(f"Foco que dispara este aviso: {aviso['source']} · {aviso['acq_datetime']:%Y-%m-%d %H:%M} UTC")

                        # rancho_row/hs_cercanos son baratos (filtrado de dataframes ya en
                        # memoria) - se calculan siempre, a diferencia del mini-mapa y el PDF
                        # (las partes caras de este bloque), que solo se generan bajo peticion
                        # explicita de un boton
                        rancho_row = ranchos.loc[ranchos["ranch_id"] == aviso["ranch_id"]].iloc[0]
                        hs_cercanos = _hotspots_cercanos_de(rancho_row, hotspots)

                        col_centrar_btn, col_mapa_btn, col_pdf_btn = st.columns(3)

                        # mismo mecanismo que el boton "🔎 Centrar mapa general" de la cabecera
                        # del aviso (session_state["foco_ranch_id"]) - se repite aqui, junto al
                        # resto de botones de abajo, para no tener que volver a subir al
                        # principio de la tarjeta tras haberla desplegado y leido. Texto/help
                        # deliberadamente distintos de los del mini-mapa de abajo (mismo icono
                        # de lupa pero "mapa GENERAL" vs "mapa del INCENDIO") - son dos mapas
                        # distintos y antes se llamaban de forma casi identica ("Ver en mapa" /
                        # "Centrar vista mapa" / "Mostrar mapa"), facil de confundir para alguien
                        # que no conoce ya la app
                        with col_centrar_btn:
                            if st.button(
                                "🔎 Centrar mapa general", key=f"centrar_btn_{aviso['ranch_id']}", width="stretch",
                                help="Centra y hace zoom en el MAPA GRANDE de la izquierda sobre esta finca (no genera nada nuevo, solo mueve la vista).",
                            ):
                                st.session_state["foco_ranch_id"] = aviso["ranch_id"]
                                st.rerun()

                        # el mini-mapa es lo mas caro de este bloque (construye un Folium
                        # completo) - solo se genera bajo peticion explicita de este boton, no
                        # automaticamente al desplegar el expander: st.expander no expone de
                        # forma fiable su estado abierto/cerrado via session_state (por eso antes
                        # solo el aviso #1 -el que viene expanded=True de fabrica- mostraba mapa)
                        key_mapa_visible = f"mapa_visible_{aviso['ranch_id']}"
                        if key_mapa_visible not in st.session_state:
                            st.session_state[key_mapa_visible] = (i == 0)
                        mostrar_mapa = st.session_state[key_mapa_visible]
                        with col_mapa_btn:
                            if st.button(
                                "🔥 Ocultar mapa del incendio" if mostrar_mapa else "🔥 Ver mapa del incendio",
                                key=f"toggle_mapa_{aviso['ranch_id']}", width="stretch",
                                help="Genera un mapa APARTE, de cerca, solo de este incendio: anillos de seguridad y focos cercanos a la finca.",
                            ):
                                st.session_state[key_mapa_visible] = not mostrar_mapa
                                st.rerun()

                        # informe PDF: flujo en 2 pasos (generar -> descargar), igual que el
                        # mapa, para no reconstruirlo en cada rerun mientras el aviso esta
                        # desplegado - genera llamadas de red propias (Open-Meteo + teselas de
                        # satelite), mas lento que el mini-mapa, de ahi mantenerlo aparte
                        key_pdf = f"pdf_bytes_{aviso['ranch_id']}"
                        with col_pdf_btn:
                            if st.button("📄 Generar informe PDF", key=f"gen_pdf_{aviso['ranch_id']}", width="stretch"):
                                with st.spinner("Generando informe PDF (mapas + meteo)..."):
                                    hs_mismo_fuego = _hotspots_mismo_fuego_de(aviso, hotspots)
                                    hs_vista_general = _hotspots_vista_general_de(rancho_row, hotspots)
                                    st.session_state[key_pdf] = generar_pdf_aviso(
                                        rancho_row, aviso, hs_cercanos, hs_mismo_fuego, hs_vista_general,
                                    )
                            if key_pdf in st.session_state:
                                st.download_button(
                                    "⬇ Descargar informe PDF", data=st.session_state[key_pdf],
                                    file_name=f"informe_incendio_{aviso['ranch_name']}.pdf".replace(" ", "_"),
                                    mime="application/pdf", key=f"dl_pdf_{aviso['ranch_id']}", width="stretch",
                                )

                        if st.session_state[key_mapa_visible]:
                            # nota: la ultima posicion de los animales (obtener_ultimas_posiciones,
                            # src/ranches.py) se quito de este repo publico - requiere BD en vivo,
                            # que este repo no tiene por diseno; se mantiene en la copia interna
                            # ixo-geospacial, con acceso a la BD de produccion
                            m_mini = _mapa_mini_aviso(rancho_row, aviso, hs_cercanos)
                            st_folium(m_mini, width=None, height=480, returned_objects=[], key=f"mapa_ranking_{aviso['ranch_id']}")

    # ---- LISTA DE GANADERIAS EN RIESGO (mismas que el ranking) ----
    with tab_lista:
        with st.container(height=PANEL_HEIGHT, border=False):
            if avisos_filtrados is None or avisos_filtrados.empty:
                st.info("Ninguna ganadería con foco activo dentro de los filtros actuales.")
            else:
                st.caption("Mismas ganaderías que el ranking de riesgo. Encabezados de columna: clic para ordenar.")

                tabla_lista = avisos_filtrados.copy()
                tabla_lista["estado_foco"] = tabla_lista["ultima_deteccion"].apply(
                    lambda ud: _estado_foco(ud)[2]
                )

                columnas_lista = ["ranch_name", "customer_name", "customer_phone", "zona_nombre",
                                   "tipo_ganaderia", "region", "distance_km", "direction_es",
                                   "risk_level", "ring", "estado_foco", "lugar", "acq_datetime"]
                tabla_lista = tabla_lista[columnas_lista].rename(columns={
                    "ranch_name": "Ganadería", "customer_name": "Cliente", "customer_phone": "Teléfono",
                    "zona_nombre": "Zona", "tipo_ganaderia": "Tipo", "region": "Región",
                    "distance_km": "Distancia (km)", "direction_es": "Dirección", "risk_level": "Riesgo",
                    "ring": "Anillo", "estado_foco": "Estado", "lugar": "Localización del foco",
                    "acq_datetime": "Detección (UTC)",
                })
                tabla_lista["_orden"] = tabla_lista["Riesgo"].map(ORDEN_RIESGO)
                tabla_lista["_urgencia"] = tabla_lista["Estado"].map(ORDEN_URGENCIA).fillna(3)
                tabla_lista = tabla_lista.sort_values(
                    ["_orden", "_urgencia", "Distancia (km)"]
                ).drop(columns=["_orden", "_urgencia"])

                st.dataframe(tabla_lista, width="stretch", height=PANEL_HEIGHT - 90)

                st.download_button(
                    "⬇ Descargar (CSV)",
                    data=tabla_lista.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"avisos_incendios_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv",
                    mime="text/csv",
                )

                # st.dataframe no admite botones dentro de sus filas - se anade debajo una
                # lista compacta con un boton de informe PDF real por cada ganaderia, mismas
                # filas que la tabla de arriba (ordenadas igual por nivel de riesgo/distancia)
                st.markdown("---")
                st.caption("Informe individual en PDF por ganadería:")
                lista_pdf = avisos_filtrados.copy()
                lista_pdf["_orden"] = lista_pdf["risk_level"].map(ORDEN_RIESGO)
                lista_pdf["_urgencia"] = lista_pdf["ultima_deteccion"].apply(_orden_urgencia)
                lista_pdf = lista_pdf.sort_values(["_orden", "_urgencia", "distance_km"]).reset_index(drop=True)

                for _, aviso_fila in lista_pdf.iterrows():
                    col_nombre, col_gen, col_dl = st.columns([3, 1.3, 1.3])
                    with col_nombre:
                        st.write(f"**{aviso_fila['ranch_name']}** — {aviso_fila['customer_name']} · {aviso_fila['risk_level']}")

                    rancho_fila = ranchos.loc[ranchos["ranch_id"] == aviso_fila["ranch_id"]].iloc[0]
                    key_pdf_fila = f"pdf_bytes_lista_{aviso_fila['ranch_id']}"
                    with col_gen:
                        if st.button("📄 Generar", key=f"gen_pdf_lista_{aviso_fila['ranch_id']}", width="stretch"):
                            with st.spinner("Generando informe PDF..."):
                                hs_cercanos_fila = _hotspots_cercanos_de(rancho_fila, hotspots)
                                hs_mismo_fuego_fila = _hotspots_mismo_fuego_de(aviso_fila, hotspots)
                                hs_vista_general_fila = _hotspots_vista_general_de(rancho_fila, hotspots)
                                st.session_state[key_pdf_fila] = generar_pdf_aviso(
                                    rancho_fila, aviso_fila, hs_cercanos_fila, hs_mismo_fuego_fila,
                                    hs_vista_general_fila,
                                )
                    with col_dl:
                        if key_pdf_fila in st.session_state:
                            st.download_button(
                                "⬇ PDF", data=st.session_state[key_pdf_fila],
                                file_name=f"informe_incendio_{aviso_fila['ranch_name']}.pdf".replace(" ", "_"),
                                mime="application/pdf", key=f"dl_pdf_lista_{aviso_fila['ranch_id']}", width="stretch",
                            )
