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
- **Hard exclusions:** each layer has its own threshold (100% by default). A zone is removed if any switched-on layer covers at least that layer's threshold share of it. Brownfield coverage is estimated as the zone's brownfield hectares divided by its area. Removed zones are greyed out and left out of the ranking and clustering.
- **Scoring:** each feature is converted to a percentile. The switched-on features are combined as a weighted sum (weights from −1 to 1), and the result is shown as a percentile among the zones that aren't excluded. Red means low and green means high.
- **Clustering:** "Cluster areas" runs scikit-learn k-means (default settings, 1–10 clusters) on the feature percentiles of the zones that aren't excluded. The cluster profiles panel shows each cluster's centre.
- **Agent (`agent.py`):** "Ask the agent" on the map opens a chat window. You describe what you want to see. Claude, running through the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) on your local Claude Code login, does the rest:
  1. investigates the data with in-process tools (`describe_data`, `preview_config`)
  2. writes a config, which is saved to `configs/`
  3. loads the config into the map (`apply_config`)
  4. explains its choices

  The agent has no other tools: no shell, file or web access. Follow-up messages continue the same conversation, and "New chat" starts a fresh one.
- **Front end (`index.html`):** a single file using deck.gl and MapLibre. No build step.

## Caveats

- Inner-London MSOAs are often smaller than an H3 cell at resolution 8. Their base features are therefore smoothed across neighbouring MSOAs, but their policy and exclusion values are exact.
- A missing CBD distance is set to the median, and other missing values are set to 0.
- K-means uses a fixed random seed (`random_state=0`), so the same settings always give the same clusters.
- Summed features favour larger zones.
