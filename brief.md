Project Brief: Homes England Greenfield Site Selection GIS Prototype
1. Executive Summary
Homes England requires an automated, data-driven GIS workflow to identify and rank viable greenfield sites for sustainable housing developments. This prototype will demonstrate a Multi-Criteria Decision Analysis (MCDA) model built entirely on UK Open Government Data, enabling fast, repeatable site-screening across target local authorities.
2. Core Objectives
Automate Site Filtering: Instantly drop non-viable land using automated hard-exclusion boundaries (e.g., flood zones, protected habitats).
Rank Viability: Score surviving land parcels on connectivity, topography, service access, and policy alignment.
Deliver an Open-Source MVP: Build a prototype using free, open-access spatial datasets under the Open Government Licence (OGL).
3. Spatial Data Architecture (Public Datasets)
                                  INPUT LAYERS
                                       │
     ┌─────────────────────────────────┼─────────────────────────────────┐
     ▼                                 ▼                                 ▼
 HARD EXCLUSIONS                   SUITABILITY SCORING             STRATEGIC CONTEXT
 (Cutout Geometry)                 (Weighted Overlay)              (Filter & Flag)
 ├─ EA Flood Zones 2 & 3           ├─ ALC Grades (3b–5 preferred)   ├─ DLUHC Planning Data
 ├─ Defra MAGIC (SSSI, SAC, SPA)   ├─ OS Terrain 50 (Slopes < 5°)   ├─ Public Sector Land (Land Hub)
 ├─ Historic England (Monuments)   ├─ OS Open Roads (Proximity)     └─ Green Belt / "Grey Belt"
 └─ OS Open Zoomstack (Built-up)   └─ DfT Journey Time Data
4. Processing Rules & MCDA Scoring Criteria
Rule Type
Data Layer
Criteria / Threshold
Action
Hard Exclusion
Flood Risk
Environment Agency Zones 2 & 3
Clip out geometry (0 score)
Hard Exclusion
Protected Habitats
SSSIs, SACs, SPAs, Ancient Woodland
Clip out geometry (0 score)
Hard Exclusion
Terrain Slope
OS Terrain 50 slope > 15°
Clip out geometry (0 score)
Weighted Suitability
Agricultural Land
Grade 1 & 2 penalized; Grade 3b–5 prioritized
Score scale (0–100)
Weighted Suitability
Transport Distance
< 800m to OS Open Road A-roads/Bus stops
Score scale (0–100)
Weighted Suitability
Public Services
Travel times to GP surgeries & primary schools
Score scale (0–100)
Strategic Flag
Site Area Threshold
Minimum contiguous parcel size ≥ 1.5 Ha
Polygon area filter


. Deliverables & Tech Stack
Frontend Mockup / Visualization: Interactive web map to similulate the GIS pipelines
Accompanying presale slides with data attributes and assumptions, timelines to build an actual MVP and product 5

