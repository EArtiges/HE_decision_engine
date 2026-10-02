"""One-off data preparation for the decision engine.

Downloads open data layers (cached in data/raw/), then builds one feature table per zone level
in data/levels/<level>.parquet, which app.py loads at startup.

Run: uv run prepare_data.py
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import geopandas as gpd
import h3
import numpy as np
import pandas as pd
import shapely

DATA = Path(__file__).parent / "data"
RAW, LEVELS = DATA / "raw", DATA / "levels"
BNG = 27700  # British National Grid, metres: all areas are computed in this CRS

PLANNING_API = "https://www.planning.data.gov.uk/entity.geojson"
MSOA_API = ("https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
            "Middle_layer_Super_Output_Areas_December_2021_Boundaries_EW_BGC_V3/FeatureServer/0/query")

# Hard-exclusion groups -> planning.data.gov.uk datasets.
EXCLUSIONS = {
    "flood": ["flood-risk-zone"],  # EA Flood Zones 2 & 3
    "habitats": ["site-of-special-scientific-interest", "special-area-of-conservation",
                 "special-protection-area", "ancient-woodland"],
    "monuments": ["scheduled-monument"],
}

# Overture features kept from the base dataset: Core + Amenities.
SUMMED = {"population", "station_count"}


def get_json(url, retries=4):
    for attempt in range(retries):
        try:
            with urlopen(url, timeout=600) as r:
                return json.load(r)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def fetch_planning_tile(dataset, bounds):
    x0, y0, x1, y1 = bounds
    wkt = f"POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"
    features, offset = [], 0
    while True:
        q = urlencode({"dataset": dataset, "geometry": wkt, "geometry_relation": "intersects",
                       "limit": 500, "offset": offset})
        page = get_json(f"{PLANNING_API}?{q}")["features"]
        features += page
        if len(page) < 500:
            return features
        offset += 500


def planning_layer(dataset, london):
    """All features of a planning.data.gov.uk dataset intersecting London, clipped to London. Cached."""
    path = RAW / f"{dataset}.parquet"
    if path.exists():
        return gpd.read_parquet(path)
    # Small tiles queried in parallel: large bbox queries time out on heavy layers (flood zones).
    x0, y0, x1, y1 = london.total_bounds
    xs, ys = np.arange(x0, x1, 0.1), np.arange(y0, y1, 0.07)
    tiles = [(x, y, min(x + 0.1, x1), min(y + 0.07, y1)) for x in xs for y in ys]
    print(f"  {dataset}: {len(tiles)} tiles…", flush=True)
    with ThreadPoolExecutor(8) as pool:
        features = [f for page in pool.map(lambda t: fetch_planning_tile(dataset, t), tiles) for f in page]
    gdf = gpd.GeoDataFrame.from_features(features, crs=4326)
    gdf = gdf.drop_duplicates("entity")
    gdf["geometry"] = shapely.make_valid(gdf.geometry.values)
    gdf = gdf.clip(london.union_all())
    gdf.to_parquet(path)
    print(f"  {dataset}: {len(gdf)} features", flush=True)
    return gdf


def london_msoas(borough_names):
    path = RAW / "msoa.parquet"
    if path.exists():
        return gpd.read_parquet(path)
    features, offset = [], 0
    while True:
        q = urlencode({"where": "1=1", "outFields": "MSOA21CD,MSOA21NM", "outSR": 4326, "f": "geojson",
                       "geometry": "-0.52,51.28,0.34,51.70", "geometryType": "esriGeometryEnvelope",
                       "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "resultOffset": offset})
        page = get_json(f"{MSOA_API}?{q}")["features"]
        if not page:
            break
        features += page
        offset += len(page)
    gdf = gpd.GeoDataFrame.from_features(features, crs=4326).drop_duplicates("MSOA21CD")
    # MSOA names are "<local authority> <nnn>": keep the London boroughs.
    gdf = gdf[gdf.MSOA21NM.str.rsplit(" ", n=1).str[0].isin(borough_names)]
    gdf.to_parquet(path)
    return gdf


def chop(geoms, size=1000):
    """Union of `geoms` (BNG), cut into tiles of `size` metres.

    Unioned layers can be a few huge polygons (e.g. the Thames floodplain); intersecting zones against
    small tiles instead is orders of magnitude faster. Pieces are disjoint, so their areas can be summed.
    """
    union = shapely.union_all(geoms)
    if union.is_empty:
        return gpd.GeoSeries([], crs=BNG)
    x0, y0, x1, y1 = union.bounds
    boxes = shapely.box(*np.meshgrid(np.arange(x0, x1, size), np.arange(y0, y1, size)),
                        *np.meshgrid(np.arange(x0, x1, size) + size, np.arange(y0, y1, size) + size))
    boxes = boxes.ravel()
    union_parts = gpd.GeoSeries(shapely.get_parts(union), crs=BNG)
    bi, ui = union_parts.sindex.query(boxes, predicate="intersects")
    pieces = shapely.intersection(boxes[bi], union_parts.values[ui])
    return gpd.GeoSeries(pieces[shapely.area(pieces) > 0], crs=BNG)


def coverage(zones, parts):
    """Share of each zone's area covered by `parts` (disjoint pieces from chop(), both in BNG)."""
    if len(parts) == 0:
        return np.zeros(len(zones))
    zi, pi = parts.sindex.query(zones.values, predicate="intersects")
    covered = np.bincount(zi, shapely.area(shapely.intersection(zones.values[zi], parts.values[pi])), len(zones))
    return np.clip(covered / shapely.area(zones.values), 0, 1)


def area_weighted(zones, cells, values, features):
    """Apportion base-cell values to zones by overlapping area: totals split by area share, others area-averaged."""
    zi, ci = cells.sindex.query(zones.values, predicate="intersects")
    a = shapely.area(shapely.intersection(zones.values[zi], cells.values[ci]))
    w = a / shapely.area(cells.values[ci])
    X = values[ci]
    out = {}
    for j, c in enumerate(features):
        if c in SUMMED or c.startswith("poi_"):
            out[c] = np.bincount(zi, w * X[:, j], len(zones))
        else:
            out[c] = np.bincount(zi, a * X[:, j], len(zones)) / np.bincount(zi, a, len(zones))
    return pd.DataFrame(out)


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    LEVELS.mkdir(parents=True, exist_ok=True)

    base = gpd.read_parquet(DATA / "london_physical_features.geoparquet")
    features = ["population", "station_count", "cbd_distance_m"] + [c for c in base.columns if c.startswith("poi_")]
    raw = base[features].copy()
    raw["cbd_distance_m"] = raw["cbd_distance_m"].fillna(raw["cbd_distance_m"].median())
    raw = raw.fillna(0.0)

    boroughs = gpd.read_file(DATA / "london_boroughs.geojson")
    london = boroughs[["geometry"]]

    print("Downloading layers (cached in data/raw/)…", flush=True)
    layers = {ds: planning_layer(ds, london) for ds in
              [d for ds in EXCLUSIONS.values() for d in ds] + ["green-belt", "brownfield-land"]}
    msoa = london_msoas(set(boroughs.LAD23NM))

    # Zone geometries per level, in WGS84, indexed by display id.
    cells = gpd.GeoSeries(base.geometry.values, index=base.h3_id, crs=4326)
    parents = sorted({h3.cell_to_parent(h, 7) for h in base.h3_id})
    zones = {
        "h3_8": cells,
        "h3_7": gpd.GeoSeries([shapely.Polygon([(lng, lat) for lat, lng in h3.cell_to_boundary(h)]) for h in parents],
                              index=parents, crs=4326),
        "msoa": gpd.GeoSeries(msoa.geometry.values, index=msoa.MSOA21NM.values, crs=4326),
        "boroughs": gpd.GeoSeries(boroughs.geometry.values, index=boroughs.LAD23NM.values, crs=4326),
    }

    cells_bng = cells.to_crs(BNG)
    # Union + tile each exclusion group once; reused for every zone level.
    group_parts = {g: chop(pd.concat([layers[d] for d in ds]).to_crs(BNG).geometry.values) for g, ds in EXCLUSIONS.items()}
    green_belt = chop(layers["green-belt"].to_crs(BNG).geometry.values)
    brownfield = layers["brownfield-land"].to_crs(BNG)
    brownfield["hectares"] = pd.to_numeric(brownfield["hectares"], errors="coerce").fillna(0.0)

    for key, z in zones.items():
        print(f"Building level {key} ({len(z)} zones)…", flush=True)
        z_bng = z.to_crs(BNG)
        df = raw.reset_index(drop=True) if key == "h3_8" else area_weighted(z_bng, cells_bng, raw.to_numpy(), features)
        df["green_belt_pct"] = coverage(z_bng, green_belt) * 100
        zi, pi = brownfield.sindex.query(z_bng.values, predicate="contains")
        df["brownfield_ha"] = np.bincount(zi, brownfield.hectares.values[pi], len(z))
        for g, parts in group_parts.items():
            df["cov_" + g] = coverage(z_bng, parts)
        # Brownfield sites are points with an area: coverage approximated as site hectares / zone area.
        df["cov_brownfield"] = np.clip(df["brownfield_ha"].to_numpy() * 1e4 / shapely.area(z_bng.values), 0, 1)
        geom = z.values if key.startswith("h3") else shapely.simplify(z.values, 0.0002, preserve_topology=True)
        out = gpd.GeoDataFrame(df, geometry=geom, crs=4326)
        out.insert(0, "zone_id", z.index.values)
        out.to_parquet(LEVELS / f"{key}.parquet")
    print("Done.")


if __name__ == "__main__":
    main()
