"""Search regions -- counties and their neighbours.

Keyed to how a flipper actually works: a home county plus the counties they
would realistically drive to. Adjacency is geographic, not metro-based.
"""
from __future__ import annotations

# county key -> (display name, query string, adjacent county keys)
COUNTIES: dict[str, dict] = {
    "washington": {
        "name": "Washington County, OK",
        "query": "Washington County, OK",
        "seat": "Bartlesville",
        "adjacent": ["nowata", "osage", "rogers", "tulsa"],
    },
    "nowata": {
        "name": "Nowata County, OK",
        "query": "Nowata County, OK",
        "seat": "Nowata",
        "adjacent": ["washington", "rogers", "craig"],
    },
    "osage": {
        "name": "Osage County, OK",
        "query": "Osage County, OK",
        "seat": "Pawhuska",
        "adjacent": ["washington", "tulsa", "pawnee", "kay", "noble"],
    },
    "rogers": {
        "name": "Rogers County, OK",
        "query": "Rogers County, OK",
        "seat": "Claremore",
        "adjacent": ["washington", "nowata", "tulsa", "mayes", "wagoner", "craig"],
    },
    "tulsa": {
        "name": "Tulsa County, OK",
        "query": "Tulsa County, OK",
        "seat": "Tulsa",
        "adjacent": ["washington", "osage", "rogers", "wagoner", "creek"],
    },
    "craig": {
        "name": "Craig County, OK",
        "query": "Craig County, OK",
        "seat": "Vinita",
        "adjacent": ["nowata", "rogers", "mayes"],
    },
    "mayes": {
        "name": "Mayes County, OK",
        "query": "Mayes County, OK",
        "seat": "Pryor Creek",
        "adjacent": ["rogers", "craig", "wagoner"],
    },
    "pawnee": {
        "name": "Pawnee County, OK",
        "query": "Pawnee County, OK",
        "seat": "Pawnee",
        "adjacent": ["osage", "creek", "noble", "payne"],
    },
    "creek": {
        "name": "Creek County, OK",
        "query": "Creek County, OK",
        "seat": "Sapulpa",
        "adjacent": ["tulsa", "osage", "pawnee", "okmulgee"],
    },
    "wagoner": {
        "name": "Wagoner County, OK",
        "query": "Wagoner County, OK",
        "seat": "Wagoner",
        "adjacent": ["tulsa", "rogers", "mayes"],
    },
    "kay": {
        "name": "Kay County, OK",
        "query": "Kay County, OK",
        "seat": "Newkirk",
        "adjacent": ["osage", "noble"],
    },
    "noble": {
        "name": "Noble County, OK",
        "query": "Noble County, OK",
        "seat": "Perry",
        "adjacent": ["osage", "kay", "pawnee", "payne"],
    },
}

DEFAULT_REGION = "washington"

# Tulsa County is adjacent to Washington County but is a full metro of its own:
# including it by default buries the Bartlesville-area results under thousands
# of city listings. Kept opt-in.
LARGE_METRO = {"tulsa"}


def resolve(home: str = DEFAULT_REGION, include_adjacent: bool = True,
            include_metro: bool = False,
            extra: list[str] | None = None) -> list[dict]:
    """Return the county records to search."""
    home = (home or DEFAULT_REGION).strip().lower()
    if home not in COUNTIES:
        raise KeyError(
            f"Unknown county '{home}'. Known: {', '.join(sorted(COUNTIES))}"
        )

    keys = [home]
    if include_adjacent:
        keys += [k for k in COUNTIES[home]["adjacent"] if k in COUNTIES]
    for k in (extra or []):
        k = k.strip().lower()
        if k in COUNTIES and k not in keys:
            keys.append(k)

    if not include_metro:
        keys = [k for k in keys if k == home or k not in LARGE_METRO]

    from . import prefs as _prefs
    p = _prefs.load()
    keys = [k for k in keys if k == home or not _prefs.county_excluded(k, p)]

    seen, out = set(), []
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        out.append({"key": k, **COUNTIES[k]})
    return out
