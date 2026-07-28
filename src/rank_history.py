import json
import os

import pandas as pd

from .config import DATA_DIR, RANK_HISTORY_SHEET_ID

# guarda el ranking de la ULTIMA consulta de hotspots (no cada rerun de Streamlit) para poder
# mostrar flechas de subida/bajada de posicion en el ranking de riesgo de app.py - ver
# conversacion 2026-07-28. Un JSON simple basta: una fila por ranch_id, se sobreescribe entero
# en cada ciclo de datos nuevo (no hace falta historial mas alla del ciclo anterior)
RANK_HISTORY_PATH = DATA_DIR / "ranking_historial.json"

# si RANK_HISTORY_SHEET_ID esta configurada, el historial se guarda en esa Google Sheet en vez
# de en el archivo local - Streamlit Community Cloud no tiene disco persistente, y sin esto el
# ranking se resetea cada vez que el contenedor se reinicia (duerme por inactividad o se
# redespliega). Un unico worksheet con el JSON entero en una celda basta, no hace falta una fila
# por rancho.
_HOJA_NOMBRE = "ranking_historial"
_CELDA = "A1"


def _cliente_sheets():
    """Cliente gspread autenticado con la MISMA cuenta de servicio ya usada para Google Earth
    Engine (GEE_SERVICE_ACCOUNT_EMAIL/GEE_SERVICE_ACCOUNT_KEY_JSON, ver src/gee_hotspots.py) -
    solo hace falta ademas: 1) habilitar la Google Sheets API en el mismo proyecto de GCP, y
    2) compartir la hoja (RANK_HISTORY_SHEET_ID) con ese email como Editor. Devuelve None si
    RANK_HISTORY_SHEET_ID no esta configurada o faltan las credenciales - en ese caso se usa el
    archivo local (ver mas abajo)."""
    if not RANK_HISTORY_SHEET_ID:
        return None
    sa_email = os.getenv("GEE_SERVICE_ACCOUNT_EMAIL")
    sa_key_json = os.getenv("GEE_SERVICE_ACCOUNT_KEY_JSON")
    if not (sa_email and sa_key_json):
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(sa_key_json)
    credenciales = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gspread.authorize(credenciales)


def _abrir_hoja(cliente):
    import gspread

    spreadsheet = cliente.open_by_key(RANK_HISTORY_SHEET_ID)
    try:
        return spreadsheet.worksheet(_HOJA_NOMBRE)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=_HOJA_NOMBRE, rows=1, cols=1)


def _cargar_historial() -> dict:
    try:
        cliente = _cliente_sheets()
    except Exception as e:
        cliente = None
        print(f"[rank_history] fallo autenticando con Google Sheets: {type(e).__name__}: {e}")

    if cliente is not None:
        try:
            valor = _abrir_hoja(cliente).acell(_CELDA).value
            return json.loads(valor) if valor else {"fetched_at": None, "ranks": {}}
        except Exception as e:
            # Google Sheets configurado pero fallo puntual (rate limit, hoja no compartida con
            # la cuenta de servicio, sin red...) - se cae al archivo local en vez de romper el
            # ranking de la app entera por un problema de persistencia
            print(f"[rank_history] fallo leyendo historial de Google Sheets: {type(e).__name__}: {e}")

    if not RANK_HISTORY_PATH.exists():
        return {"fetched_at": None, "ranks": {}}
    try:
        return json.loads(RANK_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"fetched_at": None, "ranks": {}}


def _guardar_historial(fetched_at_iso: str, ranks: dict) -> None:
    cuerpo = json.dumps({"fetched_at": fetched_at_iso, "ranks": ranks}, ensure_ascii=False)

    try:
        cliente = _cliente_sheets()
    except Exception as e:
        cliente = None
        print(f"[rank_history] fallo autenticando con Google Sheets: {type(e).__name__}: {e}")

    if cliente is not None:
        try:
            _abrir_hoja(cliente).update_acell(_CELDA, cuerpo)
            return  # guardado en Sheets con exito - no hace falta duplicar tambien en local
        except Exception as e:
            print(f"[rank_history] fallo escribiendo historial en Google Sheets: {type(e).__name__}: {e}")

    RANK_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    RANK_HISTORY_PATH.write_text(cuerpo, encoding="utf-8")


def calcular_cambios_ranking(ranking_actual: pd.DataFrame, fetched_at) -> dict:
    """Compara el orden actual del ranking (ya ordenado por riesgo/urgencia/distancia, una fila
    por ganaderia) contra el guardado en el ciclo de datos anterior, y devuelve
    {ranch_id: (tipo, delta)} con tipo en {"nuevo", "sube", "baja", "igual"} - en el primerisimo
    arranque (sin historial en disco todavia) toda ganaderia sale como "nuevo", igual que
    cualquier ganaderia que entre en el ranking por primera vez en ciclos posteriores.

    `fetched_at` es el timestamp (UTC) de la consulta de hotspots vigente (src/config._cargar_
    hotspots* en app.py, cacheada 15 min) - el historial solo se sobreescribe cuando este
    timestamp cambia respecto al guardado, de forma que las flechas se mantienen estables
    mientras Streamlit re-renderiza con los mismos hotspots en cache, y solo se recalculan
    quando de verdad llega un ciclo de datos nuevo (o al dia siguiente)."""
    fetched_at_iso = fetched_at.isoformat() if fetched_at is not None else None
    historial = _cargar_historial()
    ranks_previos = historial.get("ranks") or {}

    ranks_actuales = {str(rid): i + 1 for i, rid in enumerate(ranking_actual["ranch_id"])}

    cambios = {}
    for rid, rank_actual in ranks_actuales.items():
        rank_previo = ranks_previos.get(rid)
        if rank_previo is None:
            cambios[rid] = ("nuevo", None)
        elif rank_previo > rank_actual:
            cambios[rid] = ("sube", rank_previo - rank_actual)
        elif rank_previo < rank_actual:
            cambios[rid] = ("baja", rank_actual - rank_previo)
        else:
            cambios[rid] = ("igual", 0)

    if fetched_at_iso is not None and historial.get("fetched_at") != fetched_at_iso:
        _guardar_historial(fetched_at_iso, ranks_actuales)

    return cambios
