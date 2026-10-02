"""Homes England investment decision engine — PoC backend.

Run: uv run app.py  ->  http://localhost:8000
"""
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sklearn.cluster import KMeans

HERE = Path(__file__).parent
DATA = HERE / "data" / "levels"  # built by prepare_data.py

LEVEL_LABELS = {"h3_8": "H3 8", "h3_7": "H3 7", "msoa": "MSOA", "boroughs": "Boroughs"}
BASE_LEVEL = "h3_8"
EXCLUSIONS = {"flood": "Flood zones 2 & 3", "habitats": "Protected habitats", "monuments": "Scheduled monuments",
              "brownfield": "Brownfield land"}


def ring(coords):
    return [[round(x, 5), round(y, 5)] for x, y in coords]


def load_level(key):
    g = gpd.read_parquet(DATA / f"{key}.parquet")
    feats = [c for c in g.columns if c not in ("zone_id", "geometry") and not c.startswith("cov_")]
    return feats, {
        # A zone can have several polygons (e.g. boroughs split by the Thames).
        "cells": [{"id": z, "polys": [ring(p.exterior.coords) for p in shapely.get_parts(geom)]}
                  for z, geom in zip(g.zone_id, g.geometry)],
        "raw": g[feats].round(1).to_numpy().tolist(),
        # Percentile-rank each feature to [0, 1] so weights are comparable despite skew/units.
        "norm": g[feats].rank(pct=True).to_numpy(dtype=np.float64),
        "cov": {c[4:]: g[c].to_numpy() for c in g.columns if c.startswith("cov_")},
    }


LEVELS = {}
for key in LEVEL_LABELS:
    FEATURES, LEVELS[key] = load_level(key)
IDX = {c: i for i, c in enumerate(FEATURES)}

# How base cells were combined into larger zones (area-weighted, see prepare_data.py).
SUMMED = {"population", "station_count", "brownfield_ha"}
AGG = {c: "sum" if c in SUMMED or c.startswith("poi_") else "mean" for c in FEATURES}
LABELS = {"cbd_distance_m": "CBD distance (m)", "green_belt_pct": "Green Belt coverage (%)",
          "brownfield_ha": "Brownfield land (ha)"}


def group(c):
    if c.startswith("poi_"):
        return "Amenities (POI count)"
    if c in ("green_belt_pct", "brownfield_ha"):
        return "Policy"
    return "Core"


def label(c):
    s = LABELS.get(c) or c.removeprefix("poi_").replace("_", " ")
    return s[0].upper() + s[1:]


DEFAULTS = {"population": 0.6, "cbd_distance_m": -0.4, "brownfield_ha": 0.5, "poi_health_care": 0.3}

app = FastAPI()


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/meta")
def meta(level: str = BASE_LEVEL):
    L = LEVELS[level]
    return {
        "levels": [{"key": k, "label": v} for k, v in LEVEL_LABELS.items()],
        "exclusions": [{"key": k, "label": v} for k, v in EXCLUSIONS.items()],
        "features": [{"key": c, "label": label(c), "group": group(c), "default": DEFAULTS.get(c), "agg": AGG[c]} for c in FEATURES],
        "cells": L["cells"],
        "raw": L["raw"],
        "cov": {g: np.round(L["cov"][g], 3).tolist() for g in EXCLUSIONS},  # for the tooltip
    }


class Weights(BaseModel):
    weights: dict[str, float]
    level: str = BASE_LEVEL
    exclusions: dict[str, float] = {}  # enabled exclusion layer -> coverage threshold


def excluded_mask(body):
    """Zones where any enabled exclusion layer covers at least its own threshold."""
    L = LEVELS[body.level]
    mask = np.zeros(len(L["norm"]), dtype=bool)
    for g, t in body.exclusions.items():
        if g in EXCLUSIONS:
            mask |= L["cov"][g] >= t - 1e-6  # tolerance so fully covered zones count at 100%
    return mask


def with_gaps(values, mask):
    """Full-length list with None for excluded zones."""
    out = [None] * len(mask)
    for i, v in zip(np.flatnonzero(~mask), values):
        out[i] = v
    return out


@app.post("/score")
def score(body: Weights):
    w = np.zeros(len(FEATURES))
    for k, v in body.weights.items():
        if k in IDX:
            w[IDX[k]] = v
    mask = excluded_mask(body)
    norm = LEVELS[body.level]["norm"][~mask]
    # Percentiles are ranked among the zones that survive the exclusions.
    pct = pd.Series(norm @ w).rank(pct=True).to_numpy() if w.any() else np.full(len(norm), 0.5)
    return {"pct": with_gaps(np.round(pct, 4).tolist(), mask), "excluded": int(mask.sum())}


class ClusterRequest(Weights):
    n_clusters: int = 5


@app.post("/cluster")
def cluster(body: ClusterRequest):
    # Vector per surviving zone = percentiles of the features switched on (weights are ignored).
    keys = [k for k in body.weights if k in IDX]
    mask = excluded_mask(body)
    X = LEVELS[body.level]["norm"][~mask][:, [IDX[k] for k in keys]]
    k = max(1, min(body.n_clusters, len(X)))
    if not keys or k == 1 or len(X) == 0:
        centers = [np.round(X.mean(0) * 100, 1).tolist()] if len(X) else []
        return {"labels": with_gaps([0] * len(X), mask), "keys": keys, "centers": centers, "excluded": int(mask.sum())}
    km = KMeans(n_clusters=k).fit(X)
    # K-means label ids are arbitrary; renumber by cluster size so colours stay stable between runs.
    order = np.argsort(-np.bincount(km.labels_, minlength=k))
    return {
        "labels": with_gaps(np.argsort(order)[km.labels_].tolist(), mask),
        "keys": keys,
        "centers": np.round(km.cluster_centers_[order] * 100, 1).tolist(),  # [cluster][feature], percentile 0-100
        "excluded": int(mask.sum()),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
