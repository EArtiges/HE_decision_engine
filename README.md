# Homes England investment decision engine

Showroom PoC: a map of London scored by a weighted sum of physical features, recomputed live as you move the sliders.

## Run

```sh
uv run app.py
```

Then open http://localhost:8000. Dependencies are declared inline in `app.py`, so uv installs them on first run.

## How it works

- **Data:** `london_physical_features.geoparquet` has 52 features for 2,628 H3 cells at resolution 8. Borough boundaries in `london_boroughs.geojson` are the ONS local authority boundaries (December 2023, generalised).
- **Scoring (Python, `app.py`):** each feature is converted to a percentile, the switched-on features are combined as a weighted sum (weights from −1 to 1), and the result is shown as a percentile. Red means low and green means high.
- **Zones:** you can view H3 resolution 8, 7 or 6, or London boroughs. For the larger zones, totals are summed, densities, 500m metrics and distances are averaged, and ratios are recalculated. Each H3 cell is assigned to the borough its centre falls in.
- **Front end (`index.html`):** a single file using deck.gl and MapLibre, with a CARTO basemap. No build step.

## Caveats

- Missing values are filled in:
  - a missing distance to the nearest landmark is set to 500 m (nothing found within 500 m)
  - a missing count is set to 0
  - a missing CBD distance is set to the median
- For distance features, a higher value means further away. Use a negative weight to favour being close.
- Summed features favour larger zones, such as big boroughs and partly covered H3 cells at the edge.
