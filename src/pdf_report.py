"""Informe PDF por ganaderia, pensado para enviar directamente al cliente (no tecnico): mapa
de la situacion, datos meteorologicos del lugar, y texto interpretativo en lenguaje llano en
vez de solo cifras. Reportlab para la maquetacion, matplotlib solo para las dos imagenes
(mapa estatico y grafica de viento) que se insertan como PNG."""
import math
import os
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

# rasterio trae su propia copia de PROJ/GDAL autocontenida - si la maquina tiene OTRA
# instalacion de PROJ en el PATH/entorno (p.ej. PostgreSQL/PostGIS, muy habitual en equipos de
# desarrollo con BD local) con un proj.db de version distinta, contextily/rasterio fallan al
# reproyectar y el mapa se queda sin imagen de fondo (silenciosamente, ver _preparar_ejes_
# satelite). Idealmente esto ya lo hizo app.py al arrancar (antes de que nada importe geopandas/
# pyproj); se repite aqui como red de seguridad por si este modulo se usa de forma independiente.
# find_spec() localiza rasterio SIN ejecutarlo - un `import rasterio` normal ya dispara el
# registro interno de GDAL/PROJ en ese instante, demasiado tarde para corregir la variable.
import importlib.util as _importlib_util
_rasterio_spec = _importlib_util.find_spec("rasterio")
if _rasterio_spec and _rasterio_spec.origin:
    _rasterio_proj_dir = Path(_rasterio_spec.origin).resolve().parent / "proj_data"
    if _rasterio_proj_dir.exists():
        os.environ.setdefault("PROJ_LIB", str(_rasterio_proj_dir))
        os.environ.setdefault("PROJ_DATA", str(_rasterio_proj_dir))

import contextily as ctx
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")  # sin pantalla (Streamlit Cloud/servidor) - debe fijarse antes de pyplot
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import pandas as pd
from shapely.geometry import Point
from shapely.ops import nearest_points, unary_union
from reportlab.lib import colors as rl_colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

import matplotlib.lines as mlines
import matplotlib.patches as mpatches

from .config import HOTSPOT_AGE_BINS_H, HOTSPOT_AGE_COLORS, RING_THRESHOLDS_KM
from .fires import formatear_hace
from .geo_utils import bearing_deg
from .interpretacion import detectar_rodeado, interpretar_riesgo, interpretar_viento
from .risk import (
    ESTADO_FOCO_ACTIVO_H, ESTADO_FOCO_CONTROLADO_H, MAPA_GENERAL_ASPECT, anillos_riesgo, estado_foco,
)
from .weather import obtener_meteo_reciente

LOGO_PATH = Path(__file__).resolve().parents[1] / "Logo_ixorigue-BpQt6KE7.png"

RISK_BADGE = {
    "3/3": ("#ef4444", "RIESGO MÁXIMO"),
    "2/3": ("#f97316", "RIESGO ALTO"),
    "1/3": ("#3b82f6", "VIGILANCIA"),
}

COLORS_RING = {
    "Dentro (0 km)": "#d62728",
    "0-3 km": "#06d6a0",
    "3-5 km": "#ef476f",
    "5-10 km": "#ffd166",
}


CRS_METRICO = "EPSG:3857"


def _preparar_ejes_mapa(ax, geoms_3857, padding_frac: float = 0.2, proveedor=None, aspect_ratio: float = 1.0):
    """Encuadra los ejes a un extent que cubre `geoms_3857` (con margen) y superpone un mapa base
    (por defecto imagen de satelite Esri World Imagery, misma fuente que usa el mapa interactivo
    de la app; se puede pasar `proveedor=ctx.providers.OpenStreetMap.Mapnik` para un mapa de
    calles en vez de satelite). `aspect_ratio` (ancho/alto) por defecto da un extent CUADRADO -
    necesario porque el contenido real casi nunca es cuadrado y, sin forzarlo,
    ax.set_aspect("equal") + bbox_inches="tight" recorta la imagen final de forma desigual y el
    mapa sale estirado. Un aspect_ratio > 1 da un rectangulo mas ancho que alto (mismo numero de
    metros por cm en ambos ejes - la "escala" no cambia, solo se ve mas terreno a los lados).

    Si no hay conexion a internet o falla la descarga de teselas, se deja el mapa sin fondo en
    vez de romper la generacion del informe."""
    total = unary_union(geoms_3857)
    minx, miny, maxx, maxy = total.bounds
    dx, dy = (maxx - minx) or 200, (maxy - miny) or 200
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    lado_y = max(dx, dy) * (1 + 2 * padding_frac)
    lado_x = lado_y * aspect_ratio
    ax.set_xlim(cx - lado_x / 2, cx + lado_x / 2)
    ax.set_ylim(cy - lado_y / 2, cy + lado_y / 2)
    ax.set_aspect("equal")
    try:
        ctx.add_basemap(ax, source=proveedor or ctx.providers.Esri.WorldImagery, crs=CRS_METRICO, attribution_size=5)
    except Exception:
        pass
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)


def _color_edad_hotspot(acq_datetime) -> tuple[str, str]:
    """(label, color) segun antiguedad de la deteccion - mismos umbrales/colores que la leyenda
    del mapa interactivo de la app (src/config.py: HOTSPOT_AGE_BINS_H/HOTSPOT_AGE_COLORS)."""
    horas = (pd.Timestamp.now(tz="UTC") - acq_datetime).total_seconds() / 3600.0
    for i, umbral in enumerate(HOTSPOT_AGE_BINS_H):
        if horas <= umbral:
            label = f"\u2264{umbral}h" if i == 0 else f"{HOTSPOT_AGE_BINS_H[i - 1]}-{umbral}h"
            return label, HOTSPOT_AGE_COLORS[label]
    label = f">{HOTSPOT_AGE_BINS_H[-1]}h"
    return label, HOTSPOT_AGE_COLORS[label]


def _handles_leyenda_hotspots(hotspots_df):
    """Un handle de leyenda por cada franja de antiguedad presente en `hotspots_df` (no todas
    las franjas posibles, solo las que de verdad aparecen en el mapa)."""
    presentes = {}
    if hotspots_df is not None and not hotspots_df.empty:
        for _, hs in hotspots_df.iterrows():
            label, color = _color_edad_hotspot(hs["acq_datetime"])
            presentes[label] = color
    orden = list(HOTSPOT_AGE_COLORS.keys())
    return [
        mlines.Line2D([], [], marker="o", linestyle="", color=presentes[label],
                      markeredgecolor="white", markersize=7, label=f"Hotspot {label}")
        for label in orden if label in presentes
    ]


def _etiqueta_zona(ax, rancho_row, punto_3857):
    """Nombre de la(s) zona(s) del rancho, como etiqueta de texto bajo el marcador de la finca."""
    zona = rancho_row.get("zonas_nombres") or rancho_row.get("zona_nombre") or ""
    if not zona:
        return
    if len(zona) > 40:
        zona = zona[:37] + "..."
    txt = ax.annotate(
        zona, xy=(punto_3857.x, punto_3857.y), xytext=(0, -16), textcoords="offset points",
        ha="center", fontsize=7.5, fontweight="bold", color="white", zorder=7,
    )
    txt.set_path_effects([pe.withStroke(linewidth=2.2, foreground="black")])


def _flecha_norte(ax):
    txt = ax.annotate(
        "N", xy=(0.95, 0.92), xytext=(0.95, 0.80), xycoords="axes fraction",
        ha="center", fontsize=11, fontweight="bold", color="white",
        arrowprops=dict(arrowstyle="-|>", lw=1.8, color="white"),
    )
    txt.set_path_effects([pe.withStroke(linewidth=2.5, foreground="black")])


def _mapa_general(rancho_row, hotspots_vista_general, aviso_row) -> BytesIO:
    """Vista general de la zona sobre OpenStreetMap: la finca y TODOS los focos de calor
    activos dentro del area visible de este mapa (no solo el que dispara el aviso ni solo los
    "cercanos" en sentido estricto de anillo de 10km - `hotspots_vista_general` viene
    pre-filtrado por src.risk.bbox_vista_general(), que cubre exactamente el rectangulo alargado
    que se ve aqui, para no dejar fuera focos activos que caen en los bordes laterales). Usa la
    MISMA escala/zoom (mismos metros por cm) que _mapa_anillos (extent del anillo de 10km, mismo
    padding) para que ambos mapas sean directamente comparables - solo cambia el mapa base
    (calles en vez de satelite) y la forma (rectangulo alargado en vez de cuadrado, para ocupar
    el ancho completo de la primera pagina junto con la cabecera y el texto introductorio)."""
    rancho_geom = rancho_row["geometry"]
    fig, ax = plt.subplots(figsize=(11, 11 / MAPA_GENERAL_ASPECT))

    rancho_3857 = gpd.GeoSeries([rancho_geom], crs="EPSG:4326").to_crs(CRS_METRICO)
    rancho_3857.plot(ax=ax, color="#ffd166", alpha=0.4, edgecolor="#7c1d0f", linewidth=2.2, zorder=4)

    # extent = anillo de 10km (igual que _mapa_anillos), no el bbox de los hotspots - asi ambos
    # mapas comparten exactamente la misma escala de vista
    outer_ring_3857 = gpd.GeoSeries([anillos_riesgo(rancho_geom)[-1][1]], crs="EPSG:4326").to_crs(CRS_METRICO).iloc[0]

    if hotspots_vista_general is not None and not hotspots_vista_general.empty:
        pts_3857 = gpd.GeoSeries(
            gpd.points_from_xy(hotspots_vista_general["longitude"], hotspots_vista_general["latitude"]),
            crs="EPSG:4326",
        ).to_crs(CRS_METRICO)
        colores_edad = [_color_edad_hotspot(dt)[1] for dt in hotspots_vista_general["acq_datetime"]]
        ax.scatter(pts_3857.x, pts_3857.y, marker="o", s=70, c=colores_edad,
                   edgecolor="white", linewidth=0.6, zorder=5)

    foco_3857 = gpd.GeoSeries([Point(aviso_row["hotspot_lon"], aviso_row["hotspot_lat"])],
                              crs="EPSG:4326").to_crs(CRS_METRICO).iloc[0]
    ax.scatter([foco_3857.x], [foco_3857.y], marker="*", s=280, color="#dc2626",
               edgecolor="white", linewidth=0.8, zorder=6)

    _preparar_ejes_mapa(
        ax, [outer_ring_3857], padding_frac=0.08, proveedor=ctx.providers.OpenStreetMap.Mapnik,
        aspect_ratio=MAPA_GENERAL_ASPECT,
    )
    _flecha_norte(ax)
    _etiqueta_zona(ax, rancho_row, rancho_3857.iloc[0].centroid)
    ax.set_title("Vista general — OpenStreetMap", fontsize=10, pad=10)

    handles = [
        mpatches.Patch(facecolor="#ffd166", alpha=0.4, edgecolor="#7c1d0f", label="Perímetro de la finca"),
        mlines.Line2D([], [], marker="*", linestyle="", color="#dc2626", markeredgecolor="white",
                      markersize=13, label="Foco que dispara este aviso"),
    ] + _handles_leyenda_hotspots(hotspots_vista_general)
    ax.legend(handles=handles, loc="upper left", fontsize=6, framealpha=0.88, edgecolor="none")

    buf = BytesIO()
    fig.savefig(buf, format="jpg", dpi=150, bbox_inches="tight", pil_kwargs={"quality": 82})
    plt.close(fig)
    buf.seek(0)
    return buf


def _mapa_anillos(rancho_row, hotspots_cercanos, aviso_row) -> BytesIO:
    """Mapa de cerca sobre imagen de satelite: perimetro de la finca, anillos de riesgo
    (3/5/10km) y SOLO los focos que caen dentro del anillo de 10km (los focos mas lejanos ya se
    ven en el mapa de vista general), con una linea de distancia hasta el foco que dispara el
    aviso - mismo contenido que el mini-mapa interactivo de la app."""
    rancho_geom = rancho_row["geometry"]
    fig, ax = plt.subplots(figsize=(7.6, 7.6))

    anillos = anillos_riesgo(rancho_geom)  # [("0-3 km", geom), ("3-5 km", geom), ("5-10 km", geom)]
    geoms_extent = []
    for km, (label, geom) in reversed(list(zip(RING_THRESHOLDS_KM, anillos))):
        anillo_3857 = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(CRS_METRICO)
        anillo_geom_3857 = anillo_3857.iloc[0]
        anillo_3857.boundary.plot(ax=ax, color=COLORS_RING[label], linestyle="--", linewidth=1.8, zorder=3)
        geoms_extent.append(anillo_geom_3857)
        # etiqueta del radio en km, en el punto mas al norte del anillo
        minx_r, _, maxx_r, maxy_r = anillo_geom_3857.bounds
        txt = ax.annotate(
            f"{km} km", xy=((minx_r + maxx_r) / 2, maxy_r), xytext=(0, 3), textcoords="offset points",
            ha="center", fontsize=7.5, fontweight="bold", color=COLORS_RING[label], zorder=6,
        )
        txt.set_path_effects([pe.withStroke(linewidth=2, foreground="black")])

    outer_ring_3857 = geoms_extent[0]  # tras el reversed(), el primero anadido es el de 10km

    rancho_3857 = gpd.GeoSeries([rancho_geom], crs="EPSG:4326").to_crs(CRS_METRICO)
    rancho_3857.plot(ax=ax, color="#ffd166", alpha=0.4, edgecolor="#7c1d0f", linewidth=2.2, zorder=4)
    centroide_3857 = rancho_3857.iloc[0].centroid

    # solo los focos DENTRO del anillo de 10km - los que quedan fuera ya se ven en el mapa de
    # vista general (_mapa_general), aqui solo interesa el detalle cercano a la finca
    hotspots_dentro = None
    if hotspots_cercanos is not None and not hotspots_cercanos.empty:
        hotspots_cercanos = hotspots_cercanos.reset_index(drop=True)
        pts_3857_todos = gpd.GeoSeries(
            gpd.points_from_xy(hotspots_cercanos["longitude"], hotspots_cercanos["latitude"]), crs="EPSG:4326",
        ).to_crs(CRS_METRICO)
        dentro_mask = pts_3857_todos.within(outer_ring_3857)
        hotspots_dentro = hotspots_cercanos[dentro_mask.values]
        pts_3857 = pts_3857_todos[dentro_mask.values]
        if not pts_3857.empty:
            colores_edad = [_color_edad_hotspot(dt)[1] for dt in hotspots_dentro["acq_datetime"]]
            ax.scatter(pts_3857.x, pts_3857.y, marker="o", s=70, c=colores_edad,
                       edgecolor="white", linewidth=0.6, zorder=5)

    foco_3857 = gpd.GeoSeries([Point(aviso_row["hotspot_lon"], aviso_row["hotspot_lat"])],
                              crs="EPSG:4326").to_crs(CRS_METRICO).iloc[0]
    # la linea sale del punto del PERIMETRO mas cercano al foco (mismo punto que usa el calculo
    # real de "Distancia" en src/risk.py:evaluar_riesgo, un Polygon.distance(Point) que mide al
    # borde, no al centro) - antes salia del centroide, dando la falsa impresion de que la
    # distancia se mide desde el centro de la finca en vez de desde su perimetro real
    punto_mas_cercano = nearest_points(rancho_3857.iloc[0], foco_3857)[0]
    ax.plot([punto_mas_cercano.x, foco_3857.x], [punto_mas_cercano.y, foco_3857.y],
            linestyle="--", color="white", linewidth=1.6, zorder=5)
    # distancia en km, en el punto medio de la linea (mismo valor que "Distancia" en la tabla
    # resumen de arriba, ya calculado en src/risk.py:evaluar_riesgo - no se recalcula aqui)
    punto_medio = ((punto_mas_cercano.x + foco_3857.x) / 2, (punto_mas_cercano.y + foco_3857.y) / 2)
    txt_dist = ax.annotate(
        f"{aviso_row['distance_km']:.1f} km", xy=punto_medio, xytext=(0, 5), textcoords="offset points",
        ha="center", fontsize=8, fontweight="bold", color="white", zorder=7,
    )
    txt_dist.set_path_effects([pe.withStroke(linewidth=2.2, foreground="black")])
    ax.scatter([foco_3857.x], [foco_3857.y], marker="*", s=300, color="#dc2626",
               edgecolor="white", linewidth=0.8, zorder=6)
    ax.scatter([centroide_3857.x], [centroide_3857.y], marker="o", s=55, color="#1d4ed8",
               edgecolor="white", linewidth=0.6, zorder=6)

    # fecha/hora del foco concreto que dispara este aviso, junto a su estrella - distinta de la
    # fecha/hora del "foco mas reciente en la zona" del titulo (ese puede ser OTRO hotspot mas
    # nuevo del mismo incendio, este es especificamente el que genero la alerta)
    ts_foco_aviso = aviso_row["acq_datetime"].tz_convert("Europe/Madrid").strftime("%d/%m %H:%M")
    txt_foco = ax.annotate(
        ts_foco_aviso, xy=(foco_3857.x, foco_3857.y), xytext=(8, 8), textcoords="offset points",
        ha="left", fontsize=7.5, fontweight="bold", color="#dc2626", zorder=7,
    )
    txt_foco.set_path_effects([pe.withStroke(linewidth=2, foreground="white")])

    _preparar_ejes_mapa(ax, geoms_extent, padding_frac=0.08)
    _flecha_norte(ax)
    _etiqueta_zona(ax, rancho_row, centroide_3857)

    # foco mas reciente DENTRO del anillo de 10km (o el que dispara el aviso si no hay ninguno
    # dentro) - fecha/hora + "hace X h Y min" como subtitulo, para saber de un vistazo la
    # antiguedad real del dato sin tener que ir a buscarla en el texto de mas abajo
    if hotspots_dentro is not None and not hotspots_dentro.empty:
        foco_mas_reciente = hotspots_dentro["acq_datetime"].max()
    else:
        foco_mas_reciente = aviso_row["acq_datetime"]
    ts_reciente = foco_mas_reciente.tz_convert("Europe/Madrid").strftime("%d/%m %H:%M %Z")
    ax.set_title(
        "Zonas de seguridad (anillos de 3/5/10 km) — imagen de satélite (Esri World Imagery)\n"
        f"Foco más reciente en la zona: {ts_reciente} ({formatear_hace(foco_mas_reciente)})",
        fontsize=10, pad=10,
    )

    handles = [
        mpatches.Patch(facecolor="#ffd166", alpha=0.4, edgecolor="#7c1d0f", label="Perímetro de la finca"),
        mlines.Line2D([], [], marker="o", linestyle="", color="#1d4ed8", markeredgecolor="white",
                      markersize=7, label="Centro de la finca"),
        mlines.Line2D([], [], marker="*", linestyle="", color="#dc2626", markeredgecolor="white",
                      markersize=13, label="Foco que dispara este aviso"),
    ] + _handles_leyenda_hotspots(hotspots_dentro)
    ax.legend(handles=handles, loc="upper left", fontsize=6.5, framealpha=0.88, edgecolor="none")

    buf = BytesIO()
    fig.savefig(buf, format="jpg", dpi=150, bbox_inches="tight", pil_kwargs={"quality": 82})
    plt.close(fig)
    buf.seek(0)
    return buf


def _grafico_viento(
    meteo_df: pd.DataFrame, bearing_foco_a_finca: float | None, bearing_finca_a_foco: float | None = None,
) -> BytesIO:
    """Radar/brujula de viento en "modo sonar": un punto por hora reciente, en la direccion hacia
    la que SOPLA el viento (no de donde viene) y a una distancia del centro proporcional a la
    velocidad en km/h - cuanto mas oscuro y mas grande el punto, mas reciente es (como un eco de
    sonar que se desvanece con el tiempo). La linea roja marca el rumbo desde el foco hacia la
    finca: si los puntos de viento caen cerca de esa linea, el viento sopla en la direccion del
    fuego hacia la finca (desfavorable); si caen lejos, no. Ademas se marca con una estrella la
    direccion en la que esta el foco activo respecto a la finca (el brujula esta centrada en la
    finca), para orientar de un vistazo donde esta el fuego sin tener que mirar el mapa."""
    fig = plt.figure(figsize=(5.4, 5.4))
    ax = fig.add_subplot(111, projection="polar")
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)

    n = len(meteo_df)
    # modo sonar: mas oscuro y mas grande = mas reciente (0=viejo/claro/pequeno, 1=reciente/
    # oscuro/grande) - un solo tono (azul) en vez de arcoiris, para que la intensidad transmita
    # antiguedad de un vistazo
    intensidades = [0.3 + 0.6 * (i / max(n - 1, 1)) for i in range(n)]
    colores = [plt.cm.Blues(x) for x in intensidades]
    tamanos = [60 + 150 * (i / max(n - 1, 1)) for i in range(n)]
    vmax = max(meteo_df["velocidad_kmh"].max(), 1) * 1.25
    ax.set_rlim(0, vmax)

    for i, (_, fila) in enumerate(meteo_df.iterrows()):
        direccion_sopla = math.radians((fila["direccion_grados"] + 180) % 360)
        ax.scatter([direccion_sopla], [fila["velocidad_kmh"]], s=tamanos[i], color=colores[i],
                   edgecolor="#1e3a5f", linewidth=0.5, zorder=5, alpha=0.92)
        # hora de cada punto (hora local), para saber a que momento corresponde cada lectura sin
        # tener que adivinarlo solo por el tono/tamano del punto
        etiqueta_hora = ax.annotate(
            fila["fecha_hora"].strftime("%H:%M"), xy=(direccion_sopla, fila["velocidad_kmh"]),
            xytext=(6, 6), textcoords="offset points", fontsize=6, color="#1e3a5f", zorder=7,
        )
        etiqueta_hora.set_path_effects([pe.withStroke(linewidth=2, foreground="white")])

    if bearing_foco_a_finca is not None:
        ang = math.radians(bearing_foco_a_finca)
        ax.plot([ang, ang], [0, vmax], color="#dc2626", linewidth=2.2, linestyle="--", zorder=4)
        ax.text(ang, vmax * 1.1, "Su finca", ha="center", va="center", fontsize=7.5,
                color="#dc2626", fontweight="bold")

    if bearing_finca_a_foco is not None:
        ang_foco = math.radians(bearing_finca_a_foco)
        r_foco = vmax * 0.55
        ax.scatter([ang_foco], [r_foco], marker="*", s=220, color="#f97316",
                   edgecolor="black", linewidth=0.6, zorder=6)
        ax.text(ang_foco, r_foco * 1.28, "Foco activo", ha="center", va="center", fontsize=7,
                color="#f97316", fontweight="bold")

    ax.set_thetagrids(range(0, 360, 45), labels=["N", "NE", "E", "SE", "S", "SO", "O", "NO"], fontsize=8)
    ax.set_title(
        "Radar de viento (modo sonar) — hacia dónde sopla\n"
        "línea roja = rumbo hacia su finca · más oscuro y grande = más reciente",
        fontsize=8, pad=18,
    )
    ax.tick_params(axis="y", labelsize=6)
    ax.text(
        0.5, -0.1, "Radio del punto = velocidad del viento en km/h  ·  Fuente: Open-Meteo (datos horarios)",
        transform=ax.transAxes, ha="center", fontsize=6.5, color="#555",
    )

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _grafico_meteo_24h(meteo_24h: pd.DataFrame) -> BytesIO:
    """Evolucion de temperatura y humedad relativa en las ultimas 24h, para dar contexto de
    maximas/minimas del dia mas alla de la media de las ultimas 6h ya mostrada en la tabla de
    KPIs. Dos ejes Y (temperatura en rojo, humedad en azul) sobre el mismo eje X de horas.
    Figura mas cuadrada que ancha (a diferencia de una serie temporal tipica) para que quede bien
    al colocarla en paralelo junto al radar de viento (cuadrado) en la misma fila de la pagina."""
    fig, ax_temp = plt.subplots(figsize=(6.6, 5.4))
    horas_local = meteo_24h["fecha_hora"].dt.strftime("%H:%M")

    ax_temp.plot(horas_local, meteo_24h["temp_c"], color="#dc2626", linewidth=2, marker="o", markersize=3)
    ax_temp.set_ylabel("Temperatura (°C)", color="#dc2626", fontsize=9)
    ax_temp.tick_params(axis="y", labelcolor="#dc2626", labelsize=8)
    ax_temp.tick_params(axis="x", labelsize=6.5, rotation=60)

    ax_hum = ax_temp.twinx()
    ax_hum.plot(horas_local, meteo_24h["humedad_pct"], color="#1d4ed8", linewidth=2, marker="o", markersize=3)
    ax_hum.set_ylabel("Humedad relativa (%)", color="#1d4ed8", fontsize=9)
    ax_hum.tick_params(axis="y", labelcolor="#1d4ed8", labelsize=8)

    ax_temp.set_title(
        "Temperatura y humedad — últimas 24h\n(Fuente: Open-Meteo)", fontsize=9, pad=10,
    )
    ax_temp.grid(axis="x", alpha=0.15)
    every = max(len(horas_local) // 8, 1)
    ax_temp.set_xticks(range(0, len(horas_local), every))
    ax_temp.set_xticklabels(horas_local[::every])
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _badge_texto(risk_level: str) -> tuple[str, str]:
    return RISK_BADGE.get(risk_level, ("#6b7280", risk_level))


COLOR_MARCA = "#7c1d0f"
COLOR_MARCA_CLARO = "#f2ede6"


def _pie_pagina(canvas, doc):
    """Numero de pagina + linea de marca, igual en todas las paginas (onPage callback de
    SimpleDocTemplate - reportlab no numera paginas automaticamente)."""
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(rl_colors.grey)
    canvas.drawString(1.8 * cm, 1 * cm, "Ixorigue — Panel de riesgo de incendio")
    canvas.drawRightString(A4[0] - 1.8 * cm, 1 * cm, f"Página {doc.page}")
    canvas.setStrokeColor(rl_colors.HexColor(COLOR_MARCA))
    canvas.setLineWidth(1.2)
    canvas.line(1.8 * cm, 1.35 * cm, A4[0] - 1.8 * cm, 1.35 * cm)
    canvas.restoreState()


def generar_pdf_aviso(
    rancho_row, aviso_row, hotspots_cercanos, hotspots_mismo_fuego=None, hotspots_vista_general=None,
) -> bytes:
    """Genera el informe PDF de un aviso concreto y devuelve los bytes (listos para
    st.download_button). No depende de BD - todo lo que usa (ranchos/avisos ya calculados,
    Open-Meteo sin API key) funciona igual en el despliegue publico que en local.

    `hotspots_vista_general` (opcional) es el conjunto de hotspots a pintar en el mapa de vista
    general (rectangulo alargado, mas ancho que el anillo de 10km) - normalmente pre-filtrado por
    src.risk.bbox_vista_general() para cubrir exactamente esa area. Si no se pasa, se usa
    `hotspots_cercanos` (el bbox mas estrecho del anillo de 10km), que sigue funcionando pero
    puede dejar fuera focos activos en los bordes laterales del mapa alargado."""
    if hotspots_vista_general is None:
        hotspots_vista_general = hotspots_cercanos
    centroide = rancho_row["geometry"].centroid
    # rumbo DESDE EL FOCO HACIA LA FINCA (no al reves) - es la direccion en la que tendria que
    # soplar el viento para empujar el fuego hacia la finca, ver interpretar_viento()
    bearing_foco_a_finca = bearing_deg(aviso_row["hotspot_lat"], aviso_row["hotspot_lon"], centroide.y, centroide.x)

    try:
        meteo_24h = obtener_meteo_reciente(centroide.y, centroide.x, horas=24)
    except Exception as e:
        # obtener_meteo_reciente() ya reintenta 3 veces ante fallos transitorios de red - si
        # aun asi falla, mejor dejar rastro en los logs del servidor que tragarselo en
        # silencio, un informe para cliente sin datos meteorologicos es dificil de diagnosticar
        # a posteriori sin saber que paso realmente aqui
        print(f"[pdf_report] fallo obteniendo meteo para ({centroide.y}, {centroide.x}): {type(e).__name__}: {e}")
        meteo_24h = None
    hay_meteo_24h = meteo_24h is not None and not meteo_24h.empty
    # las ultimas 6h (radar de viento, tabla de KPIs) son un subconjunto de las 24h ya pedidas -
    # una sola llamada a Open-Meteo en vez de dos
    meteo_df = meteo_24h.tail(6).reset_index(drop=True) if hay_meteo_24h else None
    hay_meteo = meteo_df is not None and not meteo_df.empty

    rodeado, texto_rodeado = detectar_rodeado(rancho_row["geometry"], hotspots_cercanos)
    piezas_riesgo = interpretar_riesgo(aviso_row, hotspots_mismo_fuego)
    viento = interpretar_viento(meteo_df, bearing_foco_a_finca) if hay_meteo else interpretar_viento(None, None)
    # rumbo DESDE LA FINCA HACIA EL FOCO (el inverso del anterior) - para senalar en el radar de
    # viento en que direccion esta el foco activo respecto a la finca
    bearing_finca_a_foco = bearing_deg(centroide.y, centroide.x, aviso_row["hotspot_lat"], aviso_row["hotspot_lon"])

    try:
        imagen_mapa_general = _mapa_general(rancho_row, hotspots_vista_general, aviso_row)
    except Exception:
        imagen_mapa_general = None  # sin conexion a internet (teselas de satelite) u otro fallo: no rompe el informe
    try:
        imagen_mapa_anillos = _mapa_anillos(rancho_row, hotspots_cercanos, aviso_row)
    except Exception:
        imagen_mapa_anillos = None
    imagen_viento = _grafico_viento(meteo_df, bearing_foco_a_finca, bearing_finca_a_foco) if hay_meteo else None
    try:
        imagen_meteo_24h = _grafico_meteo_24h(meteo_24h) if hay_meteo_24h else None
    except Exception:
        imagen_meteo_24h = None

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=1.3 * cm, bottomMargin=1.6 * cm, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
    )
    styles = getSampleStyleSheet()
    estilo_normal = ParagraphStyle("normal_es", parent=styles["Normal"], fontSize=10, leading=14, spaceAfter=8)
    estilo_destacado = ParagraphStyle("destacado_es", parent=estilo_normal, fontSize=10.5, leading=15)
    estilo_matiz = ParagraphStyle(
        "matiz_es", parent=estilo_normal, fontSize=9.3, leading=13, textColor=rl_colors.HexColor("#555"),
    )
    estilo_titulo = ParagraphStyle(
        "titulo_es", parent=styles["Heading1"], fontSize=17, textColor=rl_colors.HexColor(COLOR_MARCA), spaceAfter=2,
    )
    estilo_subtitulo = ParagraphStyle(
        "subtitulo_es", parent=styles["Heading2"], fontSize=12.5, spaceBefore=12, spaceAfter=6,
        textColor=rl_colors.HexColor(COLOR_MARCA),
    )
    estilo_pie_informe = ParagraphStyle("footer_es", parent=styles["Normal"], fontSize=7.5, textColor=rl_colors.grey)
    estilo_pie_titulo = ParagraphStyle("footer_titulo_es", parent=estilo_pie_informe, fontName="Helvetica-Bold")

    color_badge, label_badge = _badge_texto(aviso_row["risk_level"])
    _, _, estado_texto = estado_foco(aviso_row.get("ultima_deteccion"))
    # hora LOCAL DE MADRID explicitamente (no .astimezone() a secas, que usa la zona horaria del
    # sistema donde corre el proceso) - mismo bug que en src/weather.py: un servidor desplegado
    # (Streamlit Cloud, normalmente en UTC) mostraba la hora de generacion del informe 1-2h por
    # detras de la hora real de Madrid (CEST = UTC+2 en verano), confuso para el cliente que lee
    # "informe generado a las X" y no coincide con la hora que ve en su reloj
    ahora_local = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Madrid")).strftime("%d/%m/%Y %H:%M")

    def _linea(color=COLOR_MARCA, grosor=1, espacio_antes=4, espacio_despues=10):
        return HRFlowable(width="100%", thickness=grosor, color=rl_colors.HexColor(color),
                           spaceBefore=espacio_antes, spaceAfter=espacio_despues)

    def _caja(flowables, color_fondo: str, color_borde: str | None = None):
        """Tabla de una celda con fondo de color - efecto "caja de aviso" para que el texto
        clave no se pierda en un bloque uniforme de parrafos."""
        t = Table([[flowables]], colWidths=[doc.width])
        estilo = [
            ("BACKGROUND", (0, 0), (-1, -1), rl_colors.HexColor(color_fondo)),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]
        if color_borde:
            estilo.append(("BOX", (0, 0), (-1, -1), 1, rl_colors.HexColor(color_borde)))
        t.setStyle(TableStyle(estilo))
        return t

    estilo_banner = ParagraphStyle(
        "banner_es", parent=styles["Normal"], fontSize=16, textColor=rl_colors.white,
        fontName="Helvetica-Bold", alignment=1, leading=20,
    )

    story = []

    # --- cabecera: logo + titulo/fecha en la misma fila ---
    if LOGO_PATH.exists():
        cabecera_logo = Image(str(LOGO_PATH), width=3.6 * cm, height=3.6 * cm * 0.4, kind="proportional")
    else:
        cabecera_logo = Spacer(1, 0.1 * cm)
    celda_titulo = [
        Paragraph("Alerta de riesgo de incendio", estilo_titulo),
        Paragraph(f"Informe generado el {ahora_local} (hora local)", estilo_pie_informe),
    ]
    cabecera = Table([[cabecera_logo, celda_titulo]], colWidths=[4 * cm, doc.width - 4 * cm])
    cabecera.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(cabecera)
    story.append(Spacer(1, 0.3 * cm))

    # --- banner de riesgo a todo el ancho, color segun nivel - lo primero que se ve ---
    banner_texto = f"{label_badge} · {aviso_row['risk_level']} — {aviso_row['ranch_name']}"
    banner = Table([[Paragraph(banner_texto, estilo_banner)]], colWidths=[doc.width])
    banner.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), rl_colors.HexColor(color_badge)),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    story.append(banner)
    story.append(Spacer(1, 0.3 * cm))

    telefono = aviso_row.get("customer_phone")
    linea_cliente = f"<b>{aviso_row['ranch_name']}</b>"
    if aviso_row.get("customer_name") and aviso_row["customer_name"] != aviso_row["ranch_name"]:
        linea_cliente += f" — {aviso_row['customer_name']}"
    if pd.notna(telefono) and telefono:
        linea_cliente += f" · Tel.: {telefono}"
    story.append(Paragraph(linea_cliente, estilo_destacado))
    story.append(Spacer(1, 0.15 * cm))

    # --- tabla resumen (el nivel de riesgo ya esta en el banner, aqui el resto de datos) ---
    datos_tabla = [
        ["Distancia", "Anillo", "Dirección", "Estado del foco"],
        [f"{aviso_row['distance_km']:.1f} km", aviso_row["ring"],
         aviso_row["direction_es"].capitalize(), estado_texto or "—"],
    ]
    tabla = Table(datos_tabla, colWidths=[doc.width / 4] * 4)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor(COLOR_MARCA_CLARO)),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.HexColor(COLOR_MARCA)),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#d8d2c8")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(tabla)
    story.append(Spacer(1, 0.25 * cm))

    zonas = rancho_row.get("zonas_nombres") or rancho_row.get("zona_nombre") or "—"
    tipo = rancho_row.get("tipo_ganaderia") or "—"
    region = rancho_row.get("region") or "—"
    story.append(Paragraph(
        f"<b>Zona(s):</b> {zonas} &nbsp;·&nbsp; <b>Tipo de ganadería:</b> {tipo} &nbsp;·&nbsp; "
        f"<b>Región:</b> {region}", estilo_normal,
    ))

    # --- que es este sistema (contexto para un lector no tecnico que recibe el PDF sin haber
    # visto nunca el panel) - bloque compacto: un parrafo corto + una leyenda visual de colores
    # para el estado del foco, en vez de 3 parrafos de texto gris apilados (poco escaneable para
    # alguien sin conocimientos de teledeteccion/incendios). El aviso de latencia se actualiza
    # para reflejar que ahora se usa FIRMS API directa (<3h, ver src/firms_api.py) como fuente
    # preferente en vez de solo el espejo de Earth Engine (24-40h observado) ---
    story.append(Spacer(1, 0.15 * cm))
    story.append(Paragraph(
        f"<b>¿Qué es este informe?</b> Ixorigue vigila de forma automática los focos de calor "
        f"detectados por satélite (NASA) alrededor de sus fincas, y le avisa cuando alguno se "
        f"acerca. Cada foco se clasifica en tres <b>anillos de seguridad</b> según su distancia "
        f"al perímetro de la finca — <b>0-3&nbsp;km</b> (riesgo máximo), <b>3-5&nbsp;km</b> "
        f"(riesgo alto) y <b>5-10&nbsp;km</b> (vigilancia) — visibles como círculos discontinuos "
        f"en el segundo mapa de este informe. Los datos se actualizan varias veces al día a "
        f"partir de los satélites VIIRS y MODIS: no es una cámara en directo, pero sí una de las "
        f"fuentes más rápidas y fiables que existen para esto.", estilo_matiz,
    ))
    story.append(Spacer(1, 0.18 * cm))

    estilo_leyenda_estado = ParagraphStyle(
        "leyenda_estado_es", parent=estilo_normal, fontSize=8.3, leading=11,
        textColor=rl_colors.white, fontName="Helvetica-Bold", alignment=1, spaceAfter=0,
    )
    badges_estado = [
        ("Activo", f"detectado hace &lt;{ESTADO_FOCO_ACTIVO_H}h", "#ef4444"),
        ("En seguimiento", f"{ESTADO_FOCO_ACTIVO_H}-{ESTADO_FOCO_CONTROLADO_H}h sin detección", "#eab308"),
        ("Controlado / extinto", f"&gt;{ESTADO_FOCO_CONTROLADO_H}h sin detección", "#22c55e"),
    ]
    tabla_estado = Table(
        [[Paragraph(f"{nombre}<br/><font size=6.5>{detalle}</font>", estilo_leyenda_estado)
          for nombre, detalle, _ in badges_estado]],
        colWidths=[doc.width / 3] * 3,
    )
    tabla_estado.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), rl_colors.HexColor(badges_estado[0][2])),
        ("BACKGROUND", (1, 0), (1, 0), rl_colors.HexColor(badges_estado[1][2])),
        ("BACKGROUND", (2, 0), (2, 0), rl_colors.HexColor(badges_estado[2][2])),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(Paragraph("<b>Estado del foco</b> (campo de la tabla de arriba y de los mapas):", estilo_matiz))
    story.append(Spacer(1, 0.08 * cm))
    story.append(tabla_estado)
    story.append(Paragraph(
        "<i>Un foco \"Controlado/extinto\" puede reactivarse si las condiciones lo favorecen — "
        "no equivale a que el peligro haya desaparecido del todo.</i>", estilo_matiz,
    ))

    # --- mapas ---
    # las imagenes se insertan con kind="proportional" (solo se fija el ancho, el alto lo
    # calcula reportlab a partir de las dimensiones reales del JPEG) - forzar ancho Y alto a un
    # mismo valor fijo (16x16cm) estiraba el mapa si el recorte de matplotlib (bbox_inches=
    # "tight", que no es exactamente cuadrado una vez se anaden titulo/leyenda) no daba un
    # JPEG perfectamente cuadrado. Ademas caption+mapa van en KeepTogether para que reportlab no
    # los separe en paginas distintas.
    story.append(_linea())
    story.append(Paragraph("Situación", estilo_subtitulo))
    if imagen_mapa_general is not None:
        # ancho fijo por debajo de doc.width (no doc.width exacto: con kind="proportional" y
        # aspect_ratio ancho/alto (MAPA_GENERAL_ASPECT), un margen de seguridad evita que el
        # mapa se salga por los laterales de la pagina si el recorte final del JPEG no encaja
        # con exactitud milimetrica en el ancho disponible. Limite de alto tambien fijado para
        # que, junto con el resto del contenido de encima, quepa entero en la pagina 1.
        ancho_mapa_general = min(doc.width - 1 * cm, 15.5 * cm)
        imagen_general_flow = Image(
            imagen_mapa_general, width=ancho_mapa_general, height=9.2 * cm, kind="proportional",
        )
        imagen_general_flow.hAlign = "CENTER"
        story.append(KeepTogether([
            Paragraph("<i>Vista general: la finca y todos los focos detectados en la zona.</i>", estilo_matiz),
            imagen_general_flow,
        ]))
        story.append(Spacer(1, 0.3 * cm))
    if imagen_mapa_anillos is not None:
        story.append(KeepTogether([
            Paragraph(
                "<i>Zonas de seguridad (3/5/10 km): línea discontinua desde el punto del perímetro más cercano hasta el foco que dispara este aviso. Solo se muestran los focos dentro del anillo de 10 km.</i>",
                estilo_matiz,
            ),
            Image(imagen_mapa_anillos, width=16 * cm, height=16 * cm, kind="proportional"),
        ]))

    # --- narrativa interpretativa, resaltada en negrita/cursiva dentro de una caja ---
    story.append(_linea())
    story.append(Paragraph("¿Qué está pasando?", estilo_subtitulo))

    # primera pieza (el hecho principal) en negrita dentro de una caja de color acorde al riesgo
    contenido_caja = [Paragraph(f"<b>{piezas_riesgo[0]}</b>", estilo_destacado)]
    for matiz in piezas_riesgo[1:]:
        contenido_caja.append(Paragraph(f"<i>{matiz}</i>", estilo_matiz))
    story.append(_caja(contenido_caja, color_fondo=COLOR_MARCA_CLARO, color_borde=color_badge))
    story.append(Spacer(1, 0.2 * cm))

    if texto_rodeado:
        estilo_rodeado = estilo_destacado if rodeado else estilo_normal
        texto_final = f"<b>{texto_rodeado}</b>" if rodeado else texto_rodeado
        story.append(Paragraph(texto_final, estilo_rodeado))

    # --- meteo: siempre en pagina propia (a peticion expresa - antes el titulo de la seccion
    # podia quedar colgando al final de la pagina 2 en vez de arrancar limpio) ---
    story.append(PageBreak())
    story.append(Paragraph("Condiciones meteorológicas del lugar", estilo_subtitulo))
    if viento["frase_velocidad"]:
        story.append(Paragraph(f"<b>{viento['frase_velocidad']}</b>", estilo_destacado))
    if viento["frase_direccion"]:
        story.append(Paragraph(f"<i>{viento['frase_direccion']}</i>", estilo_matiz))

    if hay_meteo:
        temp_media, temp_max = meteo_df["temp_c"].mean(), meteo_df["temp_c"].max()
        humedad_media, humedad_min = meteo_df["humedad_pct"].mean(), meteo_df["humedad_pct"].min()
        racha_max = meteo_df["rafaga_kmh"].max()
        precip_acum = meteo_df["precipitacion_mm"].sum()

        datos_meteo = [
            ["Temp. media", "Humedad media", "Racha máx. viento", "Precipitación (6h)"],
            [f"{temp_media:.0f}°C (máx {temp_max:.0f}°C)", f"{humedad_media:.0f}% (mín {humedad_min:.0f}%)",
             f"{racha_max:.0f} km/h", f"{precip_acum:.1f} mm"],
        ]
        tabla_meteo = Table(datos_meteo, colWidths=[doc.width / 4] * 4)
        tabla_meteo.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor(COLOR_MARCA_CLARO)),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.HexColor(COLOR_MARCA)),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#d8d2c8")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(Spacer(1, 0.15 * cm))
        story.append(tabla_meteo)
        if humedad_media < 30:
            story.append(Paragraph(
                "<i>La humedad relativa media es baja, lo que favorece la propagación del fuego.</i>", estilo_matiz,
            ))
    else:
        story.append(Paragraph("No se han podido obtener datos meteorológicos recientes para esta zona.", estilo_normal))

    # las dos graficas (radar de viento + evolucion 24h) van una al lado de la otra, en una tabla
    # de 2 columnas envuelta en KeepTogether - asi ocupan menos alto (caben en la misma pagina
    # junto con el resto de "Condiciones meteorologicas" y el disclaimer final) y reportlab las
    # mantiene siempre juntas en la misma pagina
    ancho_col_grafica = doc.width / 2 - 0.15 * cm
    celda_viento, celda_24h = [], []
    if imagen_viento is not None:
        imagen_viento_flow = Image(imagen_viento, width=ancho_col_grafica, height=ancho_col_grafica, kind="proportional")
        imagen_viento_flow.hAlign = "CENTER"
        celda_viento = [imagen_viento_flow]
    if imagen_meteo_24h is not None:
        imagen_24h_flow = Image(imagen_meteo_24h, width=ancho_col_grafica, height=ancho_col_grafica, kind="proportional")
        imagen_24h_flow.hAlign = "CENTER"
        celda_24h = [
            Paragraph("<i>Evolución de temperatura y humedad, últimas 24 horas.</i>", estilo_matiz),
            imagen_24h_flow,
        ]
    if celda_viento or celda_24h:
        tabla_graficas = Table(
            [[celda_viento or "", celda_24h or ""]], colWidths=[ancho_col_grafica, ancho_col_grafica],
        )
        tabla_graficas.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(KeepTogether([Spacer(1, 0.15 * cm), tabla_graficas]))

    # --- pie ---
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph("Aviso importante", estilo_pie_titulo))
    story.append(Paragraph(
        "Este es un informe orientativo, generado automáticamente a partir de datos satelitales "
        "y meteorológicos. No sustituye ni contradice en ningún caso las indicaciones oficiales "
        "de Protección Civil, los servicios de extinción de incendios ni las autoridades "
        "competentes: ante cualquier instrucción oficial, siga siempre esa indicación por encima "
        "de lo aquí reflejado. Los satélites usados para detectar focos de calor sobrevuelan "
        "cada punto de España solo unas pocas veces al día, por lo que puede haber cambios en la "
        "situación no reflejados aún aquí. Ante cualquier indicio de peligro inminente (humo "
        "denso, avance visible del fuego), active su plan de prevención: tenga preparada la "
        "evacuación de personas y ganado, las vías de salida despejadas, y contacte con "
        "emergencias (112) si fuera necesario. Si necesita ayuda o soporte para localizar a su "
        "ganado u otras cuestiones relacionadas con este informe, contacte con nuestro soporte "
        "en el +34 638 53 62 31 o el +34 919 53 21 48.",
        estilo_pie_informe,
    ))

    doc.build(story, onFirstPage=_pie_pagina, onLaterPages=_pie_pagina)
    buf.seek(0)
    return buf.getvalue()
