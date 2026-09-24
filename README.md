# Homes England investment decision engine

Showroom PoC: a map of London scored by a weighted sum of physical features, recomputed live as you move the sliders.

## Run

```sh
uv run app.py
```

Then open http://localhost:8000. The project is managed with [uv](https://docs.astral.sh/uv/): dependencies are in `pyproject.toml` and `uv.lock`, and `uv run` installs them on first run. Add new ones with `uv add <package>`.

## How it works

- **Data:** `london_physical_features.geoparquet` has 52 features for 2,628 H3 cells at resolution 8. Borough boundaries in `london_boroughs.geojson` are the ONS local authority boundaries (December 2023, generalised).
- **Scoring (Python, `app.py`):** each feature is converted to a percentile, the switched-on features are combined as a weighted sum (weights from −1 to 1), and the result is shown as a percentile. Red means low and green means high.
- **Clustering:** "Cluster areas" runs scikit-learn k-means (default settings, 1–10 clusters) on the percentiles of the switched-on features. Weights are ignored. Clusters are numbered by size, largest first, and coloured with matplotlib's `tab10`.
- **Zones:** you can view H3 resolution 8, 7 or 6, or London boroughs. For the larger zones, totals are summed, densities, 500m metrics and distances are averaged, and ratios are recalculated. Each H3 cell is assigned to the borough its centre falls in.
- **Front end (`index.html`):** a single file using deck.gl and MapLibre, with a CARTO basemap. No build step.

## Caveats

- Missing values are filled in:
  - a missing distance to the nearest landmark is set to 500 m (nothing found within 500 m)
  - a missing count is set to 0
  - a missing CBD distance is set to the median
- For distance features, a higher value means further away. Use a negative weight to favour being close.
- K-means has no fixed random seed, so cluster boundaries can shift a little between runs.
- Summed features favour larger zones, such as big boroughs and partly covered H3 cells at the edge.
