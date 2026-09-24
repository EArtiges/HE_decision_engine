# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "geopandas", "pyarrow", "h3"]
# ///
"""Homes England investment decision engine — PoC backend.

Run: uv run app.py  ->  http://localhost:8000
"""
import re
from pathlib import Path

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
import shapely
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

HERE = Path(__file__).parent
gdf = gpd.read_parquet(HERE / "london_physical_features.geoparquet")

FEATURES = [c for c in gdf.columns if c not in ("h3_id", "lat", "lon", "geometry")]

# Fill gaps: no POI within 500m -> distance capped at 500m; other gaps -> 0 / median.
raw = gdf[FEATURES].copy()
for c in FEATURES:
    if c.endswith("_min_distance_m"):
        raw[c] = raw[c].fillna(500.0)
    elif c == "cbd_distance_m":
        raw[c] = raw[c].fillna(raw[c].median())
    else:
        raw[c] = raw[c].fillna(0.0)

IDX = {c: i for i, c in enumerate(FEATURES)}

# Aggregation to coarser zones (parent H3 cells or boroughs): totals are summed, everything else (densities,
# 500m-buffer metrics, distances) is averaged, and ratios are recomputed from sums.
SUMMED = {"population", "station_count", "protected_m", "lane_m", "shared_m", "cycle_m", "street_m", "junction_count"}
AGG = {c: "sum" if c in SUMMED or c.startswith("poi_") else "mean" for c in FEATURES}
BASE_RES = h3.get_resolution(gdf.h3_id.iloc[0])
RESOLUTIONS = [BASE_RES, BASE_RES - 1, BASE_RES - 2]


def ring(coords):
    return [[round(x, 5), round(y, 5)] for x, y in coords]


def build_level(zone_ids, shapes):
    """zone_ids: zone per base cell (NaN = excluded); shapes: zone -> list of rings."""
    df = raw.groupby(zone_ids.values).agg(AGG)
    df["cycle_street_ratio"] = (df.cycle_m / df.street_m).replace([np.inf], np.nan).fillna(0.0)
    df["protected_share"] = (df.protected_m / df.cycle_m).fillna(0.0)
    return {
        "cells": [{"id": z, "polys": shapes(z)} for z in df.index],
        "raw": df.round(1).to_numpy().tolist(),
        # Percentile-rank each feature to [0, 1] so weights are comparable despite skew/units.
        "norm": df.rank(pct=True).to_numpy(dtype=np.float64),
    }


LEVELS = {
    str(r): build_level(
        gdf.h3_id.map(lambda h: h3.cell_to_parent(h, r)),
        lambda z: [ring((lng, lat) for lat, lng in h3.cell_to_boundary(z))],
    )
    for r in RESOLUTIONS
}

# Boroughs (ONS LAD Dec 2023, generalised): each H3 cell goes to the borough containing its centre;
# cells centred outside Greater London are dropped.
boroughs = gpd.read_file(HERE / "london_boroughs.geojson").set_index("LAD23NM")
centres = gpd.GeoDataFrame(geometry=gpd.points_from_xy(gdf.lon, gdf.lat), crs=4326)
borough_of = gpd.sjoin(centres, boroughs[["geometry"]], how="left", predicate="within")["LAD23NM"]
borough_of = borough_of[~borough_of.index.duplicated()]
LEVELS["boroughs"] = build_level(
    borough_of,
    lambda z: [ring(p.exterior.coords) for p in shapely.get_parts(boroughs.geometry[z].simplify(0.0003))],
)


def group(c):
    if c.startswith("poi_"):
        return "Amenities (POI count)"
    if c.endswith("_500m") or c.endswith("_min_distance_m"):
        return "Nearby landmarks (500m)"
    if c in ("population", "station_count", "cbd_distance_m"):
        return "Core"
    return "Streets & cycling"


def label(c):
    s = re.sub(r"^poi_", "", c)
    s = re.sub(r"_500m$", "", s)
    s = s.replace("_km_km2", " (km/km²)").replace("_km2", " (/km²)")
    s = re.sub(r"_m$", " (m)", s)
    s = s.replace("cbd", "CBD").replace("_", " ")
    return s[0].upper() + s[1:]


DEFAULTS = {"population": 0.6, "transit_importance_500m": 0.5, "greenspace_count_500m": 0.3, "cbd_distance_m": -0.4}

app = FastAPI()


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/meta")
def meta(level: str = str(BASE_RES)):
    return {
        "levels": list(LEVELS),
        "features": [{"key": c, "label": label(c), "group": group(c), "default": DEFAULTS.get(c), "agg": AGG[c]} for c in FEATURES],
        "cells": LEVELS[level]["cells"],
        "raw": LEVELS[level]["raw"],
    }


class Weights(BaseModel):
    weights: dict[str, float]
    level: str = str(BASE_RES)


@app.post("/score")
def score(body: Weights):
    w = np.zeros(len(FEATURES))
    for k, v in body.weights.items():
        if k in IDX:
            w[IDX[k]] = v
    norm = LEVELS[body.level]["norm"]
    if not w.any():
        return {"pct": [0.5] * len(norm)}
    s = norm @ w
    pct = pd.Series(s).rank(pct=True).to_numpy()
    return {"pct": np.round(pct, 4).tolist()}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
