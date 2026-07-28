import json

import pandas as pd

from .config import DATA_DIR

# guarda el ranking de la ULTIMA consulta de hotspots (no cada rerun de Streamlit) para poder
# mostrar flechas de subida/bajada de posicion en el ranking de riesgo de app.py - ver
# conversacion 2026-07-28. Un JSON simple basta: una fila por ranch_id, se sobreescribe entero
# en cada ciclo de datos nuevo (no hace falta historial mas alla del ciclo anterior)
RANK_HISTORY_PATH = DATA_DIR / "ranking_historial.json"


def _cargar_historial() -> dict:
    if not RANK_HISTORY_PATH.exists():
        return {"fetched_at": None, "ranks": {}}
    try:
        return json.loads(RANK_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"fetched_at": None, "ranks": {}}


def _guardar_historial(fetched_at_iso: str, ranks: dict) -> None:
    RANK_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    RANK_HISTORY_PATH.write_text(
        json.dumps({"fetched_at": fetched_at_iso, "ranks": ranks}, ensure_ascii=False), encoding="utf-8",
    )


def calcular_cambios_ranking(ranking_actual: pd.DataFrame, fetched_at) -> dict:
    """Compara el orden actual del ranking (ya ordenado por riesgo/urgencia/distancia, una fila
    por ganaderia) contra el guardado en el ciclo de datos anterior, y devuelve
    {ranch_id: (tipo, delta)} con tipo en {"nuevo", "sube", "baja", "igual"} - en el primerisimo
    arranque (sin historial en disco todavia) toda ganaderia sale como "nuevo", igual que
    cualquier ganaderia que entre en el ranking por primera vez en ciclos posteriores.

    `fetched_at` es el timestamp (UTC) de la consulta de hotspots vigente (src/config._cargar_
    hotspots* en app.py, cacheada 15 min) - el historial en disco solo se sobreescribe cuando
    este timestamp cambia respecto al guardado, de forma que las flechas se mantienen estables
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
