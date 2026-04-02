from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List
import geopandas as gpd
import pandas as pd

from kml_service import (
    extract_coverage_geometries,
    validate_points_against_coverage
)

app = FastAPI(title="KML Coverage API")

# -----------------------------
# CONFIG
# -----------------------------
KML_FILE = "data/cobertura.kml"
coverage_gdf = None


# -----------------------------
# STARTUP EVENT (MEJOR PRÁCTICA)
# -----------------------------
@app.on_event("startup")
def load_kml():
    global coverage_gdf

    try:
        coverage_gdf = extract_coverage_geometries(KML_FILE)

        # 🔥 OPTIMIZACIÓN: crear spatial index
        coverage_gdf.sindex

        print(f"[OK] KML cargado: {len(coverage_gdf)} zonas")

    except Exception as e:
        print(f"[ERROR] No se pudo cargar el KML: {e}")
        coverage_gdf = None


# -----------------------------
# MODELOS
# -----------------------------
class PointInput(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class PointsRequest(BaseModel):
    points: List[PointInput]


# -----------------------------
# ENDPOINT PRINCIPAL
# -----------------------------
@app.post("/validate")
def validate_points(request: PointsRequest):

    if coverage_gdf is None:
        raise HTTPException(
            status_code=500,
            detail="Cobertura no cargada"
        )

    try:
        df = pd.DataFrame([
            {"latitud": p.lat, "longitud": p.lon}
            for p in request.points
        ])

        result_df = validate_points_against_coverage(
            df,
            coverage_gdf
        )

        return {
            "count": len(result_df),
            "results": result_df.to_dict(orient="records")
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------
# ENDPOINT DEBUG (MUY ÚTIL)
# -----------------------------
@app.get("/zones")
def get_zones():
    if coverage_gdf is None:
        return {"zones": 0}

    return {
        "zones": len(coverage_gdf),
        "preview": coverage_gdf[
            ["zona_nombre", "tipo_cobertura"]
        ].head(10).to_dict(orient="records")
    }


# -----------------------------
# HEALTH CHECK
# -----------------------------
@app.get("/")
def health():
    return {
        "status": "ok",
        "kml_loaded": coverage_gdf is not None
    }
