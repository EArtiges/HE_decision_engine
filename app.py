"""Homes England investment decision engine — PoC backend.

Run: uv run app.py  ->  http://localhost:8000
"""
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sklearn.cluster import KMeans

import agent

HERE = Path(__file__).parent
DATA = HERE / "data" / "levels"  # built by prepare_data.py

LEVEL_LABELS = {"h3_8": "H3 8", "h3_7": "H3 7", "msoa": "MSOA", "boroughs": "Boroughs"}
BASE_LEVEL = "h3_8"
EXCLUSIONS = {"flood": "Flood zones 2 & 3", "habitats": "Protected habitats", "monuments": "Scheduled monuments",
              "brownfield": "Brownfield land"}


def ring(coords):
    """Convert a polygon ring to a JSON-friendly list of points rounded to 5 decimals.

    Five decimal places of a degree is about 1 m, which is ample for display and keeps the /meta payload small.

    Args:
        coords: Iterable of (x, y) coordinate pairs, e.g. a shapely exterior's ``coords``.

    Returns:
        List of ``[x, y]`` lists with each coordinate rounded to 5 decimal places.
    """
    return [[round(x, 5), round(y, 5)] for x, y in coords]


def load_level(key):
    """Load one zone level's feature table (built by prepare_data.py) into memory.

    Args:
        key: Zone level key, one of ``LEVEL_LABELS`` (e.g. ``"h3_8"`` or ``"boroughs"``).

    Returns:
        A ``(features, level)`` tuple. ``features`` lists the feature column names. ``level`` is a dict with:
        ``cells`` (zone id and polygon rings per zone, for the map), ``raw`` (feature values rounded to 1
        decimal, one row per zone), ``norm`` (percentile rank of each feature in [0, 1], as a float array of
        shape (zones, features)) and ``cov`` (exclusion layer key -> share of each zone it covers).
    """
    g =gpd.read_parquet(DATA / f"{key}.parquet")
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
    """Sidebar group a feature is listed under.

    Args:
        c: Feature column name.

    Returns:
        ``"Amenities (POI count)"`` for ``poi_*`` features, ``"Policy"`` for Green Belt and brownfield,
        otherwise ``"Core"``.
    """
    if c.startswith("poi_"):
        return "Amenities (POI count)"
    if c in ("green_belt_pct", "brownfield_ha"):
        return "Policy"
    return "Core"


def label(c):
    """Human-readable label for a feature.

    Args:
        c: Feature column name.

    Returns:
        The label from ``LABELS`` if there is one, otherwise the column name without its ``poi_`` prefix and
        with underscores as spaces, in sentence case (e.g. ``"poi_health_care"`` -> ``"Health care"``).
    """
    s =LABELS.get(c) or c.removeprefix("poi_").replace("_", " ")
    return s[0].upper() + s[1:]


DEFAULTS = {"population": 0.6, "cbd_distance_m": -0.4, "brownfield_ha": 0.5, "poi_health_care": 0.3}

app = FastAPI()


@app.get("/")
def index():
    """Serve the single-page front end.

    Returns:
        index.html as a file response.
    """
    return FileResponse(HERE / "index.html")


@app.get("/meta")
def meta(level: str = BASE_LEVEL):
    """Everything the front end needs to draw a zone level and build its sidebar.

    Args:
        level: Zone level key (defaults to ``BASE_LEVEL``).

    Returns:
        Dict with the available ``levels``, the hard ``exclusions``, the ``features`` (key, label, group,
        default weight and how base cells were aggregated), the zone ``cells`` (polygons), the ``raw`` feature
        values per zone and each exclusion layer's ``cov`` share per zone (for the tooltip).

    Raises:
        KeyError: If ``level`` is not a known zone level.
    """
    L =LEVELS[level]
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
    """Zones where any enabled exclusion layer covers at least its own threshold.

    Args:
        body: Request with ``level`` and ``exclusions`` (exclusion layer key -> coverage threshold in [0, 1]).
            Unknown layer keys are ignored.

    Returns:
        Boolean array with one entry per zone of the level, True where the zone is excluded.
    """
    L = LEVELS[body.level]
    mask = np.zeros(len(L["norm"]), dtype=bool)
    for g, t in body.exclusions.items():
        if g in EXCLUSIONS:
            mask |= L["cov"][g] >= t - 1e-6  # tolerance so fully covered zones count at 100%
    return mask


def with_gaps(values, mask):
    """Full-length list with None for excluded zones.

    Args:
        values: One value per surviving zone, in zone order.
        mask: Boolean exclusion mask from ``excluded_mask``, one entry per zone.

    Returns:
        List as long as ``mask``: the values in the positions of non-excluded zones, None elsewhere.
    """
    out = [None] * len(mask)
    for i, v in zip(np.flatnonzero(~mask), values):
        out[i] = v
    return out


@app.post("/score")
def score(body: Weights):
    """Score every zone as a weighted sum of feature percentiles.

    The score of a zone is the sum of weight x feature percentile over the weighted features. Scores are then
    turned into percentiles among the zones that survive the hard exclusions.

    Args:
        body: Feature weights in [-1, 1] (unknown keys are ignored), zone level and enabled exclusions.

    Returns:
        Dict with ``pct``, the score percentile of each zone rounded to 4 decimals (None for excluded zones,
        0.5 for all zones when every weight is zero), and ``excluded``, the number of excluded zones.
    """
    w =np.zeros(len(FEATURES))
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
    """Group the surviving zones with k-means on the percentiles of the switched-on features.

    Weights are ignored: only which features are present in ``body.weights`` matters. The seed is fixed and
    clusters are numbered from largest to smallest, so the same inputs always give the same labels and colours.

    Args:
        body: Switched-on features (as ``weights`` keys), zone level, enabled exclusions and ``n_clusters``.
            The number of clusters is capped at the number of surviving zones.

    Returns:
        Dict with ``labels`` (cluster id per zone, None for excluded zones), ``keys`` (the features used),
        ``centers`` (one row per cluster: centre percentile 0-100 of each feature in ``keys``) and
        ``excluded`` (number of excluded zones). With no features, a single cluster or no surviving zones,
        every zone gets label 0.
    """
    # Vector per surviving zone = percentiles of the features switched on (weights are ignored).
    keys = [k for k in body.weights if k in IDX]
    mask = excluded_mask(body)
    X = LEVELS[body.level]["norm"][~mask][:, [IDX[k] for k in keys]]
    k = max(1, min(body.n_clusters, len(X)))
    if not keys or k == 1 or len(X) == 0:
        centers = [np.round(X.mean(0) * 100, 1).tolist()] if len(X) else []
        return {"labels": with_gaps([0] * len(X), mask), "keys": keys, "centers": centers, "excluded": int(mask.sum())}
    km = KMeans(n_clusters=k, random_state=0).fit(X)  # fixed seed: same inputs, same clusters
    # K-means label ids are arbitrary; renumber by cluster size so colours stay stable between runs.
    order = np.argsort(-np.bincount(km.labels_, minlength=k))
    return {
        "labels": with_gaps(np.argsort(order)[km.labels_].tolist(), mask),
        "keys": keys,
        "centers": np.round(km.cluster_centers_[order] * 100, 1).tolist(),  # [cluster][feature], percentile 0-100
        "excluded": int(mask.sum()),
    }


# --- Chat agent (agent.py): tools to investigate the data and evaluate configs --------------

DESCRIPTIONS = {
    "population": "Resident population",
    "station_count": "Number of rail / Underground / Overground stations. DATA GAP: only populated for inner "
                     "London in the source data (0 in every outer borough), so it is unreliable outside inner London",
    "cbd_distance_m": "Distance to central London in metres (higher = further out)",
    "green_belt_pct": "Share of the zone designated Green Belt, in %",
    "brownfield_ha": "Brownfield land register sites, in hectares",
}
EXCLUSION_DESCRIPTIONS = {
    "flood": "Environment Agency Flood Zones 2 & 3 (river and sea flood risk)",
    "habitats": "SSSI, SAC, SPA and ancient woodland",
    "monuments": "Historic England scheduled monuments",
    "brownfield": "Brownfield land register sites (coverage estimated from site hectares / zone area)",
}


def describe(level):
    """Describe the data at one zone level, for the chat agent's describe_data tool.

    Args:
        level: Zone level key.

    Returns:
        Dict with the number of zones per level (``zone_levels``), the ``described_level``, each feature's
        label, group, aggregation, description, quartiles and share of zones at zero (``features``), the 12
        most correlated feature pairs by Spearman correlation (``most_correlated_feature_pairs``) and each
        exclusion layer's description and zone counts at 50% and full coverage (``hard_exclusions``).

    Raises:
        ValueError: If ``level`` is not a known zone level.
    """
    if level not in LEVELS:
        raise ValueError(f"Unknown zones {level!r}; use one of {list(LEVELS)}")
    L = LEVELS[level]
    raw = np.asarray(L["raw"], dtype=float)
    features = []
    for c in FEATURES:
        col = raw[:, IDX[c]]
        q = np.percentile(col, [0, 25, 50, 75, 100]).round(1).tolist()
        features.append({"key": c, "label": label(c), "group": group(c), "aggregation": AGG[c],
                         "description": DESCRIPTIONS.get(c) or f"Count of {label(c).lower()} points of interest (Overture)",
                         "min_p25_median_p75_max": q, "share_of_zones_zero": round(float((col == 0).mean()), 2)})
    corr = np.corrcoef(L["norm"].T)  # Spearman, since norm holds ranks
    pairs = sorted(((abs(corr[i, j]), FEATURES[i], FEATURES[j], round(float(corr[i, j]), 2))
                    for i in range(len(FEATURES)) for j in range(i + 1, len(FEATURES))), reverse=True)[:12]
    return {
        "zone_levels": {k: {"label": v, "zones": len(LEVELS[k]["norm"])} for k, v in LEVEL_LABELS.items()},
        "described_level": level,
        "features": features,
        "most_correlated_feature_pairs": [{"a": a, "b": b, "spearman": r} for _, a, b, r in pairs],
        "hard_exclusions": [{"key": g, "label": EXCLUSIONS[g], "description": EXCLUSION_DESCRIPTIONS[g],
                             "zones_covered_at_least_50pct": int((L["cov"][g] >= 0.5).sum()),
                             "zones_fully_covered": int((L["cov"][g] >= 1 - 1e-6).sum())} for g in EXCLUSIONS],
    }


def parse_config(cfg):
    """Config dict (export format) -> (request, mode). Raises ValueError on invalid content.

    Features and exclusions that are missing or not enabled are switched off. Numbers are clipped to their
    valid range: ``n_clusters`` to 1-10, weights to [-1, 1] and thresholds to [0.05, 1].

    Args:
        cfg: Parsed config, as written by the front end's YAML export (keys ``zones``, ``mode``,
            ``n_clusters``, ``hard_exclusions`` and ``features``).

    Returns:
        A ``(request, mode)`` tuple: a ``ClusterRequest`` usable by both ``score`` and ``cluster``, and the
        mode, ``"score"`` or ``"cluster"``.

    Raises:
        ValueError: If the zone level or mode is unknown, or a feature or exclusion key doesn't exist.
    """
    level = cfg.get("zones", BASE_LEVEL)
    if level not in LEVELS:
        raise ValueError(f"Unknown zones {level!r}; use one of {list(LEVELS)}")
    mode = cfg.get("mode", "score")
    if mode not in ("score", "cluster"):
        raise ValueError("mode must be 'score' or 'cluster'")
    feats, excl = cfg.get("features") or {}, cfg.get("hard_exclusions") or {}
    unknown = [k for k in feats if k not in IDX] + [k for k in excl if k not in EXCLUSIONS]
    if unknown:
        raise ValueError(f"Unknown keys: {unknown}")
    req = ClusterRequest(
        level=level,
        n_clusters=int(np.clip(cfg.get("n_clusters", 5), 1, 10)),
        weights={k: float(np.clip(v.get("weight", 0.5), -1, 1)) for k, v in feats.items() if v and v.get("enabled")},
        exclusions={k: float(np.clip(v.get("threshold", 1), 0.05, 1)) for k, v in excl.items() if v and v.get("enabled")},
    )
    return req, mode


def evaluate(cfg):
    """Summary of what a config produces, for the agent to check its work.

    Args:
        cfg: Parsed config in the export format (see ``parse_config``).

    Returns:
        Dict with the ``zones`` level, ``mode``, ``total_zones`` and ``excluded`` count. In cluster mode it also
        has ``cluster_sizes`` and ``cluster_centre_percentiles`` (feature -> centre percentile, per cluster). In
        score mode it has ``top_10`` and ``bottom_5``: zone id, score percentile and raw values of the enabled
        features.

    Raises:
        ValueError: If the config is invalid (see ``parse_config``).
    """
    req, mode = parse_config(cfg)
    L = LEVELS[req.level]
    ids = [c["id"] for c in L["cells"]]
    enabled = list(req.weights)
    out = {"zones": req.level, "mode": mode, "total_zones": len(ids)}
    if mode == "cluster":
        r = cluster(req)
        labels = [v for v in r["labels"] if v is not None]
        out |= {"excluded": r["excluded"], "cluster_sizes": np.bincount(labels).tolist() if labels else [],
                "cluster_centre_percentiles": [dict(zip(r["keys"], c)) for c in r["centers"]]}
        return out
    r = score(req)
    ranked = sorted((p, i) for i, p in enumerate(r["pct"]) if p is not None)
    row = lambda p, i: {"zone": ids[i], "score_percentile": round(p * 100),
                        **{f: L["raw"][i][IDX[f]] for f in enabled}}
    out |= {"excluded": r["excluded"], "top_10": [row(p, i) for p, i in ranked[::-1][:10]],
            "bottom_5": [row(p, i) for p, i in ranked[:5]]}
    return out


class ChatRequest(BaseModel):
    message: str
    config_yaml: str = ""          # the user's current sidebar settings
    session_id: str | None = None  # continue a previous conversation


@app.post("/chat")
async def chat(body: ChatRequest):
    """Streams agent events as newline-delimited JSON.

    Args:
        body: The user's message, their current sidebar config as YAML and, to continue a conversation, the
            session id returned by the previous turn.

    Returns:
        A streaming ``application/x-ndjson`` response with one JSON event per line (see ``agent.chat``).
    """
    async def events():
        """Serialise each agent event as one line of JSON.

        Yields:
            Each event from ``agent.chat`` as a JSON string ending in a newline.
        """
        async for e in agent.chat(body.message, body.config_yaml, body.session_id, describe, evaluate):
            yield json.dumps(e) + "\n"
    return StreamingResponse(events(), media_type="application/x-ndjson")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
