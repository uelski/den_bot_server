# Ingest field shape convention

**Authoritative reference for the metadata and URL shape every ingest script
targets.** Established by the URL refactor in commit `4ee6dd6`; older docs in
Qdrant follow it too (RTD was migrated at the same time).

Read this before writing a new ingest script.

## Two shapes, picked by data type

### POI shape — one doc per entity

Use for parks, libraries, rec centers, schools, RTD stops and routes — anything
where each record is a distinct entity a user might ask about by name.

```python
metadata = {
    "doc_type": "denver_<thing>",
    "service_name": "Denver <Things>",          # constant across all docs
    "display_name": <entity name>,              # per-entity (formal_name, stop_name, …)
    "base_url": DATASET_HUB_URL,                # constant — dataset /about
    "hub_url":  DATASET_HUB_URL,                # constant — same as base_url
    "map_url":  per-entity Google Maps URL,     # unique per doc
    "location": {"lat": ..., "lon": ...},       # for geo-filtering
    "has_layers": False,
    "full_metadata": json.dumps(...),
    # plus any per-entity fields you want filterable in Qdrant
}
```

**Frontend behavior:** the sources panel collapses to ONE entry per dataset
(deduped on the constant `service_name` + `base_url` + `hub_url`), while the map
viewer surfaces N entries with unique `display_name` labels and `map_url` deep
links.

**Reference:** `scripts/ingest_denver_parks.py` is canonical. Also
`ingest_denver_libraries.py`, `ingest_denver_rec_centers.py`,
`ingest_denver_public_schools.py`, `ingest_denver_non_public_schools.py`,
`ingest_rtd_gtfs.py`.

### Aggregate-per-neighborhood shape — one doc per neighborhood

Use for demographics, crime, traffic — anything rolling many incidents up into
per-neighborhood summaries.

```python
metadata = {
    "doc_type": "neighborhood_<thing>_summary",
    "service_name": "Denver <Thing>",           # constant
    # NO display_name — see below
    "neighborhood_name": <ACS NBHD_NAME>,       # for retrieval filtering
    "base_url": DATASET_HUB_URL,                # dataset /about
    "hub_url":  DATASET_HUB_URL,
    "map_url":  DATASET_EXPLORE_URL,            # dataset /explore — NOT per-neighborhood
    "location": <centroid copied from ACS geojson>,
    "has_layers": False,
    "full_metadata": json.dumps(<all stats>),
}
```

**Why no `display_name`:** every doc shares one `map_url`, so
`build_map_viewer_links` dedups them to a single map-viewer entry, and the label
would come from whichever neighborhood doc happened to sort first. "View Five
Points crime summary map" pointing at a citywide URL would mislead. Falling back
to `service_name` ("View Denver Crime Statistics map") matches the URL's actual
scope.

**Reference:** `scripts/ingest_denver_crime.py` is canonical. Also
`ingest_denver_traffic.py`, and `ingest_neighborhoods.py` for demographics.

## Shared-doc_type discriminator pattern

When two scripts produce docs that should share retrieval semantics but come
from different source datasets (public + non-public schools), use one shared
`doc_type` plus a separate discriminator field:

```python
# scripts/ingest_denver_public_schools.py
metadata["doc_type"] = "denver_school"
metadata["institution_type"] = "public"

# scripts/ingest_denver_non_public_schools.py
metadata["doc_type"] = "denver_school"
metadata["institution_type"] = "non-public"
```

**Critical: scope `--purge` on BOTH fields.** Otherwise purging one script wipes
the other's docs.

```python
def _doc_filter() -> Filter:
    return Filter(must=[
        FieldCondition(key="metadata.doc_type", match=MatchValue(value=DOC_TYPE)),
        FieldCondition(key="metadata.institution_type", match=MatchValue(value=INSTITUTION_TYPE)),
    ])
```

Always cover this with a unit test (`TestDocFilter` in the schools suites).

## Naming clash avoidance

When source data has a column whose name collides with a metadata field you want
to add, **rename ours, not the source's** — preserving source field names 1:1
keeps debugging traceable. Example: the schools source has
`SCHOOL_TYPE = "Parochial"`, so our discriminator became `institution_type`
rather than `school_type`.

## ACS neighborhood names are the canonical namespace

Anything neighborhood-anchored treats
`data/ODC_POP_ACS20172021NBRHDCOMMON.geojson` as source of truth: 78
neighborhoods, with `properties.NBHD_NAME` as the canonical name. The same
geojson seeds the resolver's `OFFICIAL_NAMES` set, demographics
`neighborhood_name` metadata, the centroids copied into crime and traffic docs,
the PIP polygon set for crime, and the validation set for traffic.

If a future source carries its own neighborhood naming, validate-against or
PIP-against this geojson rather than introducing a second namespace.

## Helpers worth reusing

- **`_clean(value)`** — in most ingest scripts. Strips whitespace, returns
  `None` for empty strings. Some scripts (rec centers) extend it to treat the
  literal `"<Null>"` as None, which the source data uses as a null sentinel.
- **`format_address(props)`** — composes `"line1[, line2], city, state[ zip]"`
  with fallbacks (city→"Denver", state→"CO"). Shared by libraries, rec centers,
  schools.
- **Sentence-builder pattern** — each piece of `page_content` is its own
  composer returning `None` when its field is missing; `build_page_content`
  joins the non-None sentences. Keeps each branch independently testable.

## Testing patterns to copy

Each script has a parallel `tests/test_ingest_<thing>.py` with:

- One class per builder function, covering missing/empty/edge-case branches
- `TestBuild<Entity>Document` — metadata key presence, `doc_type`,
  `service_name`, `display_name`, location shape, URL fields, `full_metadata`
  round-trip
- `TestBuildDocuments` — batch level: skipped invalid features, empty input
- `TestDocFilter` — shared-`doc_type` scripts only; verifies the discriminator
  condition is present

`tests/test_ingest_denver_parks.py` is the canonical reference.

## Hand-off discipline

Build the script and run pytest, then hand the actual ingest run to a human.
Never auto-run `--purge` or any data-mutating script.

## Related

- Scripts importing `worker.pipeline` directly must call `load_dotenv()` *before*
  the import — see the gotcha note in `NEXT_STEPS.md`.
- Deployment and secret handling: `deployment.md`.
