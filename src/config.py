import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]      # side_projects/alerta_incendios/
REPO_ROOT = ROOT_DIR.parents[1]                      # ixo-geospacial/
DATA_DIR = ROOT_DIR / "data"
OUTPUTS_DIR = ROOT_DIR / "outputs"

# origen de los datos de ranchos/clientes: "db" consulta produccion en vivo (uso local, requiere
# PGHOST/PGUSER/... en el entorno), "snapshot" lee un archivo local generado con
# scripts/exportar_snapshot.py - pensado para el despliegue publico, que no tiene acceso a la BD
# de produccion. Los datos de incendio (GEE) siguen siendo en tiempo real en ambos casos.
RANCHOS_DATA_SOURCE = os.getenv("RANCHOS_DATA_SOURCE", "db")
RANCHOS_SNAPSHOT_PATH = Path(os.getenv("RANCHOS_SNAPSHOT_PATH", str(DATA_DIR / "ranchos_snapshot.gpkg")))

# Google Earth Engine - proyecto propio del usuario (credenciales ya presentes en esta maquina,
# ~/.config/earthengine/credentials). Sobreescribible via .env si se usa otro proyecto/cuenta.
GEE_PROJECT = os.getenv("GEE_PROJECT", "terrasetapp")

# ventana de deteccion (horas) - igual que WINDOW_HOURS del script GEE original
WINDOW_HOURS_DEFAULT = 72

SCALE_VIIRS = 375
SCALE_MODIS = 1000

# anillos de riesgo, identicos al script GEE original (riskFromRing)
RING_THRESHOLDS_KM = [3, 5, 10]
RING_RISK = {
    "Dentro (0 km)": "3/3",
    "0-3 km": "3/3",
    "3-5 km": "2/3",
    "5-10 km": "1/3",
}

COLORS_RING = {
    "Dentro (0 km)": "#d62728",
    "0-3 km": "#06d6a0",
    "3-5 km": "#ef476f",
    "5-10 km": "#ffd166",
}

CARDINAL_ES = {
    "N": "norte", "NE": "noreste", "E": "este", "SE": "sureste",
    "S": "sur", "SO": "suroeste", "O": "oeste", "NO": "noroeste",
}

# color de cada hotspot en el mapa segun su antiguedad (horas desde la deteccion): morado (recien
# detectado, foco mas caliente/urgente) -> rojo -> naranja -> amarillo -> verde oliva apagado ->
# gris (deteccion muy antigua dentro de la ventana consultada, probablemente ya extinguido) - mas
# umbrales pasadas las 24h porque la ventana de consulta llega hasta 168h (7 dias)
HOTSPOT_AGE_BINS_H = [6, 12, 24, 48, 72]
HOTSPOT_AGE_COLORS = {
    "≤6h": "#a21caf",
    "6-12h": "#dc2626",
    "12-24h": "#f97316",
    "24-48h": "#eab308",
    "48-72h": "#65a30d",
    ">72h": "#6b7280",
}
