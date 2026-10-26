# Homes England investment decision engine

Showroom PoC: a map of London scored by a weighted sum of features. It recalculates live as you move the sliders, after removing areas that hit hard planning constraints.

## Run

```sh
uv run prepare_data.py   # one-off: downloads open data and builds data/levels/*.parquet
uv run app.py
```

Then open http://localhost:8000.

The chat agent runs in Microsoft Foundry, so run `az login` first. Your account needs the Foundry User role on the Foundry resource; the role used to be called Azure AI User. The project endpoint, model deployment and agent name default to our instance's values in `agent.py`. You can override them with `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL_DEPLOYMENT` and `FOUNDRY_AGENT_NAME`.

When working locally, set `FOUNDRY_AGENT_NAME=he-decision-engine-dev`. Each running copy of the app registers its own version of the agent. If your local copy shared the deployed app's agent name, the two would keep replacing each other's version.

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
- **Programme documents:** `refresh/docs_sync.py` downloads Homes England programme guidance from GOV.UK as Markdown (Open Government Licence). It keeps the documents in the Foundry vector store `he-programme-documents`. There are two so far:
  - the [Brownfield, Infrastructure and Land Fund](https://www.gov.uk/guidance/brownfield-infrastructure-and-land-fund)
  - the [Social and Affordable Homes Programme 2026 to 2036](https://www.gov.uk/guidance/social-and-affordable-homes-programme-sahp-2026-to-2036)

  To add another document, add an entry to `SOURCES` in `refresh/docs_sync.py` and run `uv run refresh/docs_sync.py`. Only documents whose content changed are uploaded again.

  `refresh/` is also an Azure Function (`function_app.py`) that runs the same sync every day at 06:00 UTC. It isn't deployed yet, so run the sync by hand when the guidance changes.
- **Boundaries:** ONS MSOAs (December 2021) and local authority districts (December 2023), using the generalised versions.

## How it works

- **Zones:** H3 resolution 8 and 7, MSOAs and boroughs. Values are spread from the base H3 cells to the larger zones by overlapping area: totals are split by area and the other features are averaged by area. Policy and exclusion coverage are calculated directly from the zone shapes.
- **Hard exclusions:** each layer has its own threshold (100% by default). A zone is removed if any switched-on layer covers at least that layer's threshold share of it. Brownfield coverage is estimated as the zone's brownfield hectares divided by its area. Removed zones are greyed out and left out of the ranking and clustering.
- **Scoring:** each feature is converted to a percentile. The switched-on features are combined as a weighted sum (weights from −1 to 1), and the result is shown as a percentile among the zones that aren't excluded. Red means low and green means high.
- **Clustering:** "Cluster areas" runs scikit-learn k-means (default settings, 1–10 clusters) on the feature percentiles of the zones that aren't excluded. The cluster profiles panel shows each cluster's centre.
- **Agent (`agent.py`):** "Ask the agent" on the map opens a chat window. You describe what you want to see. A [Microsoft Foundry](https://ai.azure.com/) prompt agent (`he-decision-engine`) does the rest:
  1. investigates the data with function tools that run in the app (`describe_data`, `preview_config`)
  2. writes a config, which is saved to `configs/` (on the deployed app, this folder is wiped on every restart)
  3. loads the config into the map (`apply_config`)
  4. explains its choices

  When you mention a programme ("best MSOA candidates for the BIL Fund"), the agent first finds its criteria in the programme documents, using Foundry file search over the vector store. It then maps each criterion to features and exclusions, and says which criteria the data can't represent.

  The agent has no other tools: no Foundry web search or code interpreter. The app registers the agent the first time someone chats. It creates a new version only when the instructions or tools in `agent.py` change. Follow-up messages continue the same Foundry conversation, and "New chat" starts a fresh one.
- **Front end (`index.html`):** a single file using deck.gl and MapLibre. No build step.

## Deployment

The app runs on Azure Container Apps (`he-decision-engine` in `rg-he-decision-engine`, UK South). It's built from the `Dockerfile`, and the data tables are included in the image. Its managed identity calls Foundry, so there are no keys or secrets.

[DEPLOY.md](DEPLOY.md) lists every Azure CLI command, in order, to set up a new instance, for example for a client. It also covers redeploying and troubleshooting.

Not done yet:

- **Sign-in:** the app is public until an Entra app registration is set up.
- **Blob Storage:** the data tables and saved configs are planned to move there.
- **Document refresh:** the daily Function isn't deployed.

## Caveats

- Inner-London MSOAs are often smaller than an H3 cell at resolution 8. Their base features are therefore smoothed across neighbouring MSOAs, but their policy and exclusion values are exact.
- A missing CBD distance is set to the median, and other missing values are set to 0.
- K-means uses a fixed random seed (`random_state=0`), so the same settings always give the same clusters.
- Summed features favour larger zones.
- The model deployment (`gpt-6-luna`) is GlobalStandard, so prompts can be processed outside the UK. The vector store is in UK South.
- In the source data, station count is only filled in for inner London. Every outer borough shows 0, so the feature is unreliable outside inner London, and the agent is told so.
