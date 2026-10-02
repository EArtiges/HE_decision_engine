# Homes England investment decision engine

Showroom PoC: a map of London scored by a weighted sum of features. It recalculates live as you move the sliders, after removing areas that hit hard planning constraints.

## Run

```sh
uv run prepare_data.py   # one-off: downloads open data and builds data/levels/*.parquet
uv run app.py
```

Then open http://localhost:8000.

The project is managed with [uv](https://docs.astral.sh/uv/). Dependencies are in `pyproject.toml` and `uv.lock`, and `uv run` installs them on first run. Add new ones with `uv add <package>`.

The tables in `data/levels/` are committed, so you only need to run `prepare_data.py` again if you change the data. Downloads are cached in `data/raw/`, which git ignores.

## Data

- **Base features:** `data/london_physical_features.geoparquet` is the Overture dataset of H3 cells at resolution 8. Only the Core and Amenities (POI count) features are used.
- **Planning layers:** these come from [planning.data.gov.uk](https://www.planning.data.gov.uk) (DLUHC, Open Government Licence):
  - Hard exclusions:
    - EA Flood Zones 2 & 3
    - protected habitats (SSSI, SAC, SPA and ancient woodland)
    - scheduled monuments
  - Policy features:
    - Green Belt coverage (%)
    - brownfield land register (ha)
- **Boundaries:** ONS MSOAs (December 2021) and local authority districts (December 2023), using the generalised versions.

## How it works

- **Zones:** H3 resolution 8 and 7, MSOAs and boroughs. Values are spread from the base H3 cells to the larger zones by overlapping area: totals are split by area and the other features are averaged by area. Policy and exclusion coverage are calculated directly from the zone shapes.
- **Hard exclusions:** a zone is removed if the switched-on constraint layers together cover at least the threshold share of it (50% by default). Removed zones are greyed out and left out of the ranking and clustering.
- **Scoring:** each feature is converted to a percentile. The switched-on features are combined as a weighted sum (weights from −1 to 1), and the result is shown as a percentile among the zones that aren't excluded. Red means low and green means high.
- **Clustering:** "Cluster areas" runs scikit-learn k-means (default settings, 1–10 clusters) on the feature percentiles of the zones that aren't excluded. The cluster profiles panel shows each cluster's centre.
- **Front end (`index.html`):** a single file using deck.gl and MapLibre. No build step.

## Caveats

- Inner-London MSOAs are often smaller than an H3 cell at resolution 8. Their base features are therefore smoothed across neighbouring MSOAs, but their policy and exclusion values are exact.
- A missing CBD distance is set to the median, and other missing values are set to 0.
- K-means has no fixed random seed, so cluster boundaries can shift a little between runs.
- Summed features favour larger zones.
