import sys
import unicodedata

import geopandas as gpd
import pandas as pd
from shapely import wkb
from shapely.ops import unary_union
from sqlalchemy import text

from .config import RANCHOS_DATA_SOURCE, RANCHOS_SNAPSHOT_PATH, REPO_ROOT

# filtro de "cliente activo" real: no basta con que el Rancho no este deshabilitado/borrado,
# el Customer al que pertenece tambien debe estar activo y sin borrar - en produccion hay
# ranchos con Disabled=false pero cuyo Customer esta 'suspended'/'blocked'/'inactive' (101+17+1
# de 708 en la comprobacion del 11-jul-2026), que no deberian aparecer como clientes vigentes.
# ademas, se descarta el rancho si NINGUNO de sus dispositivos ha comunicado en las ultimas
# 48h (Devices.LastSeenOn) - un rancho sin telemetria reciente no aporta valor a una alerta
# de incendio en "tiempo casi real" (probablemente collar retirado, finca de baja/vacia, etc).
QUERY_RANCHOS = text("""
    SELECT r."Id" AS ranch_id, r."Name" AS ranch_name, c."Name" AS customer_name,
           r."Region" AS region,
           COALESCE(NULLIF(c."Phone", ''), (
               SELECT bu."Phone" FROM "BaseUsers" bu
               WHERE lower(bu."Email") = lower(c."Email") AND bu."Phone" IS NOT NULL AND bu."Phone" != ''
               LIMIT 1
           )) AS customer_phone,
           ST_Y(r."Location"::geometry) AS lat, ST_X(r."Location"::geometry) AS lon
    FROM "Ranches" r
    JOIN "Customers" c ON c."Id" = r."CustomerId"
    WHERE r."Country" = 'ES' AND r."DeletedAt" IS NULL AND (r."Disabled" IS NOT TRUE)
      AND r."Location" IS NOT NULL
      AND c."Status" = 'active' AND c."DeletedAt" IS NULL
      AND EXISTS (
          SELECT 1 FROM "Devices" d
          WHERE d."RanchId" = r."Id" AND d."LastSeenOn" >= NOW() - INTERVAL '48 hours'
      )
""")

# especie animal predominante por rancho - no vive en Ranches, hay que agregarla desde Animals
# (via Devices.RanchId), un rancho puede tener varias especies mezcladas
QUERY_ESPECIES = text("""
    SELECT d."RanchId" AS ranch_id, a."Specie" AS specie, COUNT(*) AS n
    FROM "Animals" a
    JOIN "Devices" d ON d."Id" = a."DeviceId"
    WHERE a."IsDeregistered" = FALSE AND d."RanchId" IS NOT NULL
    GROUP BY d."RanchId", a."Specie"
""")

SPECIE_ES = {"cow": "Bovino", "horse": "Equino", "goat": "Caprino", "sheep": "Ovino"}

# Ranches.Region es texto libre introducido en el backend, sin validar contra una lista fija -
# en produccion aparecen variantes (con/sin acentos, mayusculas, alias como "Euskadi"/"Valencia")
# del mismo nombre de comunidad autonoma. Se normaliza a las 17 CCAA + Ceuta/Melilla para que el
# filtro agrupe bien en vez de mostrar la misma region varias veces con grafias distintas.
CCAA_CANONICAS = [
    "Andalucía", "Aragón", "Asturias", "Islas Baleares", "Canarias", "Cantabria",
    "Castilla-La Mancha", "Castilla y León", "Cataluña", "Extremadura", "Galicia",
    "La Rioja", "Comunidad de Madrid", "Región de Murcia", "Navarra", "País Vasco",
    "Comunidad Valenciana", "Ceuta", "Melilla",
]


def _sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


_CCAA_ALIASES = {
    "madrid": "Comunidad de Madrid", "comunidad de madrid": "Comunidad de Madrid",
    "murcia": "Región de Murcia", "region de murcia": "Región de Murcia",
    "pais vasco": "País Vasco", "euskadi": "País Vasco",
    "valencia": "Comunidad Valenciana", "comunitat valenciana": "Comunidad Valenciana",
    "comunidad valenciana": "Comunidad Valenciana",
    "baleares": "Islas Baleares", "illes balears": "Islas Baleares", "islas baleares": "Islas Baleares",
    "castilla la mancha": "Castilla-La Mancha", "castilla-la mancha": "Castilla-La Mancha",
    "castilla y leon": "Castilla y León",
    "asturias": "Asturias", "principado de asturias": "Asturias",
    "la rioja": "La Rioja", "rioja": "La Rioja",
    "cataluna": "Cataluña", "catalunya": "Cataluña", "cataluña": "Cataluña",
    "aragon": "Aragón",
    "andalucia": "Andalucía",
    "navarra": "Navarra", "comunidad foral de navarra": "Navarra",
    "cantabria": "Cantabria",
    "galicia": "Galicia",
    "extremadura": "Extremadura",
    "canarias": "Canarias", "islas canarias": "Canarias",
    "ceuta": "Ceuta",
    "melilla": "Melilla",
}


def _normalizar_ccaa(valor) -> str | None:
    if not valor or not str(valor).strip():
        return None
    clave = _sin_acentos(str(valor)).strip().lower()
    return _CCAA_ALIASES.get(clave, str(valor).strip())

# todas las zonas activas (perimetro o no) por rancho - se decide en Python cual usar,
# para poder caer a la union de todas si no hay ninguna marcada IsPerimeter=true
QUERY_ZONAS = text("""
    SELECT z."RanchId" AS ranch_id, z."IsPerimeter" AS is_perimeter, z."Polygon" AS polygon_wkb,
           z."Name" AS zone_name
    FROM "Zones" z
    JOIN "Ranches" r ON r."Id" = z."RanchId"
    JOIN "Customers" c ON c."Id" = r."CustomerId"
    WHERE r."Country" = 'ES' AND r."DeletedAt" IS NULL AND (r."Disabled" IS NOT TRUE)
      AND z."DeletedAt" IS NULL AND z."Active" = TRUE AND z."Polygon" IS NOT NULL
      AND c."Status" = 'active' AND c."DeletedAt" IS NULL
""")


def obtener_ranchos_es() -> gpd.GeoDataFrame:
    """Punto de entrada publico: segun config.RANCHOS_DATA_SOURCE, consulta la BD de produccion
    en vivo ("db", uso local con credenciales) o lee un snapshot local generado de antemano con
    scripts/exportar_snapshot.py ("snapshot", para el despliegue publico sin acceso a la BD).
    Los datos de incendio (Google Earth Engine) son en tiempo real en ambos casos - solo la lista
    de ranchos/clientes queda fija a la fecha del ultimo snapshot."""
    if RANCHOS_DATA_SOURCE == "snapshot":
        df = _obtener_ranchos_snapshot()
    else:
        df = _obtener_ranchos_db()

    # salvaguarda para snapshots generados antes de excluir "circulo_aproximado" (o con la
    # exportacion desactualizada): _obtener_ranchos_db() ya filtra estos ranchos, pero un
    # snapshot .gpkg es un volcado estatico que puede llevar tiempo sin regenerarse - sin este
    # filtro aqui, COLOR_FUENTE/DASH_FUENTE en app.py lanzan KeyError al no reconocer ese valor
    if "fuente_geometria" in df.columns:
        df = df[df["fuente_geometria"] != "circulo_aproximado"].copy()

    return df


def _obtener_ranchos_snapshot() -> gpd.GeoDataFrame:
    if not RANCHOS_SNAPSHOT_PATH.exists():
        raise FileNotFoundError(
            f"RANCHOS_DATA_SOURCE=snapshot pero no existe {RANCHOS_SNAPSHOT_PATH}. Este repo es "
            f"la version de despliegue publico: el snapshot se genera aparte, en el monorepo "
            f"interno ixo-geospacial (side_projects/alerta_incendios/scripts/exportar_snapshot.py, "
            f"con acceso a la BD de produccion), y se copia manualmente aqui como "
            f"data/ranchos_snapshot.gpkg (no se commitea, ver README)."
        )
    return gpd.read_file(RANCHOS_SNAPSHOT_PATH)


def _obtener_ranchos_db() -> gpd.GeoDataFrame:
    """Ranchos ES activos con geometria real, en orden de preferencia:
    1. Zones.Polygon marcada IsPerimeter=true (perimetro real dibujado por el cliente).
    2. Union de todas las Zones activas del rancho (subdivisiones de pasto - aproxima el area
       realmente usada, aunque puede no cubrir todo el limite de la finca).

    Los ranchos sin ninguna zona dibujada (solo centroide en Ranches.Location) se descartan aqui:
    un circulo aproximado sobre un punto no representa el limite real de la finca y no aporta
    valor a una alerta de incendio - ver docs/plan_accion.md / conversacion 2026-07-24.

    Columna `fuente_geometria` indica cual de las 2 se uso, para dibujarlas distinto en el mapa.

    NOTA: este repo de despliegue publico no incluye db_connection.py ni credenciales de BD a
    proposito - este modo ("db") solo existe/funciona en el monorepo interno ixo-geospacial. Aqui
    solo deberia usarse RANCHOS_DATA_SOURCE=snapshot.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from db_connection import get_engine  # noqa: E402
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "RANCHOS_DATA_SOURCE=db, pero este repo de despliegue publico no tiene "
            "db_connection.py (a proposito, para no requerir credenciales de BD aqui). Usa "
            "RANCHOS_DATA_SOURCE=snapshot, o ejecuta esto desde el monorepo interno "
            "ixo-geospacial si de verdad necesitas consultar la BD en vivo."
        ) from e

    engine = get_engine()
    with engine.connect() as conn:
        df_ranchos = pd.read_sql(QUERY_RANCHOS, conn)
        df_zonas = pd.read_sql(QUERY_ZONAS, conn)
        df_especies = pd.read_sql(QUERY_ESPECIES, conn)

    df_especies["specie_es"] = df_especies["specie"].map(SPECIE_ES).fillna(df_especies["specie"].str.title())
    tipo_por_rancho = (
        df_especies.sort_values("n", ascending=False)
        .groupby("ranch_id")["specie_es"]
        .apply(lambda s: ", ".join(dict.fromkeys(s)))
    )
    df_ranchos["tipo_ganaderia"] = df_ranchos["ranch_id"].map(tipo_por_rancho).fillna("Sin especificar")
    df_ranchos["region"] = df_ranchos["region"].apply(_normalizar_ccaa)

    def _cargar_wkb(b):
        # psycopg2 devuelve geometry como str hex o memoryview segun el driver/version
        geom = wkb.loads(b, hex=True) if isinstance(b, str) else wkb.loads(bytes(b))
        # algunas Zones.Polygon dibujadas a mano son topologicamente invalidas
        # (autointersecciones) - buffer(0) las repara sin cambiar la forma visible
        return geom if geom.is_valid else geom.buffer(0)

    df_zonas["geom"] = df_zonas["polygon_wkb"].apply(_cargar_wkb)

    perimetros = (
        df_zonas[df_zonas["is_perimeter"] == True]  # noqa: E712
        .drop_duplicates("ranch_id")
        .set_index("ranch_id")[["geom", "zone_name"]]
    )
    uniones = df_zonas.groupby("ranch_id")["geom"].apply(unary_union)
    nombres_union = df_zonas.groupby("ranch_id")["zone_name"].apply(
        lambda s: ", ".join(dict.fromkeys(n for n in s if n))
    )

    # descarta los ranchos sin ninguna zona (ni perimetro ni union) antes de construir
    # geometria - no interesan en este dashboard, ver docstring de la funcion
    df_ranchos = df_ranchos[
        df_ranchos["ranch_id"].isin(set(perimetros.index) | set(uniones.index))
    ].copy()

    # mapeo vectorizado en vez de un .apply(axis=1) por fila: perimetro tiene prioridad,
    # y para los ranchos sin perimetro se rellena con la union de zonas
    ids = df_ranchos["ranch_id"]
    df_ranchos["geometry"] = ids.map(perimetros["geom"]).fillna(ids.map(uniones))
    df_ranchos["fuente_geometria"] = ids.isin(perimetros.index).map(
        {True: "perimetro_dibujado", False: "union_de_zonas"}
    )
    df_ranchos["zona_nombre"] = (
        ids.map(perimetros["zone_name"]).fillna(ids.map(nombres_union)).fillna("")
    )
    # nombres de TODAS las zonas activas del rancho (perimetro + resto), no solo la usada
    # como geometria - para mostrar en el tooltip del mapa independientemente de fuente_geometria
    df_ranchos["zonas_nombres"] = ids.map(nombres_union).fillna("")

    return gpd.GeoDataFrame(df_ranchos, geometry="geometry", crs="EPSG:4326")

# nota: obtener_ultimas_posiciones() (ultima posicion GPS de los animales por rancho, via
# LocationsHistory) se quito de este repo publico - requiere BD en vivo, que este repo no
# tiene por diseno (ver docstring de _obtener_ranchos_db). Vive en la copia interna
# ixo-geospacial/side_projects/alerta_incendios, con acceso a la BD de produccion.
