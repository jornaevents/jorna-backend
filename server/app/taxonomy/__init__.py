"""The service taxonomy (docs/DECISIONS.md #35): groups → services →
attributes, from data.py, and the ways the rest of the app reads it.

Every picker selects a service. A group key works anywhere a service key
does where "any of these" makes sense — search, a bundle slot, a client's
"not sure yet" — and `expand` turns it into its services. Today's
category/subcategory pairs map onto it through `from_legacy` and
`legacy_search`, so stored data and older clients keep working while the
app moves over.

Checked when imported: keys unique across groups and services, every service
in a group, every attribute defined, every legacy pair claimed once — so a
bad edit to data.py fails the test run and the deploy, not a request.
"""

import hashlib
import json
import re

from app.taxonomy.data import ATTRIBUTES, GROUP_LEGACY, GROUPS, LEGACY_BUNDLE_SLOTS, LEGACY_CATEGORIES, SERVICES

_GROUPS = {g["key"]: g for g in GROUPS}
_SERVICES = {s["key"]: s for s in SERVICES}
_BY_GROUP: dict[str, list[str]] = {g["key"]: [] for g in GROUPS}
_LEGACY_PAIRS: dict[tuple[str, str | None], str] = {}


class TaxonomyError(ValueError):
    pass


def _check() -> None:
    clash = set(_GROUPS) & set(_SERVICES)
    if clash or len(_GROUPS) != len(GROUPS) or len(_SERVICES) != len(SERVICES):
        raise TaxonomyError(f"Taxonomy keys must be unique across groups and services: {clash or 'a duplicate'}")
    for s in SERVICES:
        if s["group"] not in _GROUPS:
            raise TaxonomyError(f"Service {s['key']} is in unknown group {s['group']}")
        _BY_GROUP[s["group"]].append(s["key"])
        for a in s["attributes"]:
            if a not in ATTRIBUTES:
                raise TaxonomyError(f"Service {s['key']} has unknown attribute {a}")
        for pair in s["legacy"]:
            if pair in _LEGACY_PAIRS:
                raise TaxonomyError(f"Legacy pair {pair} is claimed by {_LEGACY_PAIRS[pair]} and {s['key']}")
            _LEGACY_PAIRS[pair] = s["key"]
    for g, keys in _BY_GROUP.items():
        if not keys:
            raise TaxonomyError(f"Group {g} has no services")
    for cat, rule in LEGACY_CATEGORIES.items():
        for key in rule["search"]:
            if key not in _GROUPS and key not in _SERVICES:
                raise TaxonomyError(f"Legacy category {cat} searches unknown key {key}")
        if rule["service"] is not None and rule["service"] not in _SERVICES:
            raise TaxonomyError(f"Legacy category {cat} maps to unknown service {rule['service']}")
    if set(GROUP_LEGACY) != set(_GROUPS):
        raise TaxonomyError("Every group needs a legacy category (GROUP_LEGACY)")
    for slot, key in LEGACY_BUNDLE_SLOTS.items():
        if key not in _GROUPS and key not in _SERVICES:
            raise TaxonomyError(f"Bundle slot {slot} maps to unknown key {key}")


_check()


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


# ── Lookups ──────────────────────────────────────────────────────────

def is_service(key: str) -> bool:
    return key in _SERVICES


def is_group(key: str) -> bool:
    return key in _GROUPS


def service(key: str) -> dict | None:
    return _SERVICES.get(key)


def group_of(service_key: str) -> str | None:
    s = _SERVICES.get(service_key)
    return s["group"] if s else None


def label(key: str) -> str | None:
    """A service's or a group's display name."""
    found = _SERVICES.get(key) or _GROUPS.get(key)
    return found["label"] if found else None


def expand(*keys: str) -> list[str]:
    """Service keys, in taxonomy order, for any mix of service and group
    keys. Unknown keys are dropped."""
    wanted: set[str] = set()
    for key in keys:
        if key in _SERVICES:
            wanted.add(key)
        elif key in _GROUPS:
            wanted.update(_BY_GROUP[key])
    return [s["key"] for s in SERVICES if s["key"] in wanted]


def bundle_services() -> list[str]:
    """Services the bundle builder fills on its own."""
    return [s["key"] for s in SERVICES if s["bundle"]]


def bundle_groups() -> list[str]:
    """Groups the bundle builder can fill as one slot."""
    return [g["key"] for g in GROUPS if g.get("bundle")]


# ── Today's category/subcategory ─────────────────────────────────────

def from_legacy(category: str | None, subcategory: str | None = None) -> str | None:
    """The service a stored (category, subcategory) means, or None when it's
    ambiguous (a category whose services the vendor has to pick between) or
    unknown. A subcategory that isn't its own service falls back to its
    category's rule."""
    if not category:
        return None
    if (category, subcategory) in _LEGACY_PAIRS:
        return _LEGACY_PAIRS[(category, subcategory)]
    rule = LEGACY_CATEGORIES.get(category)
    return rule["service"] if rule else None


def legacy_search(category: str) -> list[str]:
    """Service keys an old category (or subcategory, or service) key covers
    as a search filter or link. Empty for an unknown key."""
    if category in LEGACY_CATEGORIES:
        return expand(*LEGACY_CATEGORIES[category]["search"])
    if category in _SERVICES or category in _GROUPS:
        return expand(category)
    return sorted({s for (_, sub), s in _LEGACY_PAIRS.items() if sub == category})


def to_legacy(service_key: str) -> tuple[str, str | None] | None:
    """The (category, subcategory) older clients and stored rows use for a
    service — its first legacy pair. None for a service with none (new in
    the taxonomy), which older clients can't show."""
    s = _SERVICES.get(service_key)
    return s["legacy"][0] if s and s["legacy"] else None


def legacy_pair(service_key: str) -> tuple[str, str | None]:
    """What older clients and the legacy columns read for a service: its own
    legacy pair, or its group's closest category."""
    return to_legacy(service_key) or (GROUP_LEGACY[_SERVICES[service_key]["group"]], None)


def bundle_slot(old_slot: str) -> str | None:
    """The service or group a bundle slot key used today now means."""
    return LEGACY_BUNDLE_SLOTS.get(old_slot)


# ── What GET /taxonomy serves ────────────────────────────────────────

def _attribute_view(key: str) -> dict:
    a = ATTRIBUTES[key]
    out = {"key": key, "label": a["label"], "type": a["type"]}
    if "options" in a:
        out["options"] = [{"key": _slug(o), "label": o} for o in a["options"]]
    if "unit" in a:
        out["unit"] = a["unit"]
    return out


def _build_view() -> dict:
    body = {
        "groups": [
            {
                "key": g["key"],
                "label": g["label"],
                "bundle": bool(g.get("bundle")),
                "services": [
                    {
                        "key": s["key"],
                        "label": s["label"],
                        "synonyms": s["synonyms"],
                        "popular": s["popular"],
                        "bundle": s["bundle"],
                        "attributes": s["attributes"],
                    }
                    for s in SERVICES if s["group"] == g["key"]
                ],
            }
            for g in GROUPS
        ],
        "attributes": {k: _attribute_view(k) for k in ATTRIBUTES},
    }
    version = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return {"version": version, **body}


_VIEW = _build_view()


def view() -> dict:
    """The whole taxonomy for pickers, with a version that changes whenever
    any of it does (the endpoint's ETag)."""
    return _VIEW



# ── A vendor's specialties and a package's service ───────────────────

MAX_SPECIALTIES = 20


def clean_specialties(keys: list[str] | None) -> list[str]:
    """Known service keys, de-duplicated in order. Raises on an unknown key
    or too many — what a write sends is checked, not silently trimmed."""
    out: list[str] = []
    for key in keys or []:
        if key not in _SERVICES:
            raise TaxonomyError(f"Unknown service '{key}'")
        if key not in out:
            out.append(key)
    if len(out) > MAX_SPECIALTIES:
        raise TaxonomyError(f"No more than {MAX_SPECIALTIES} services")
    return out


def specialties_from_pairs(pairs: list[dict] | None, keep: list[str] | None = None) -> list[str]:
    """Specialties for a vendor saved the old way (category/subcategory
    pairs). An ambiguous category maps to nothing on its own, but a
    specialty the vendor already picked inside it (`keep`) stays."""
    out: list[str] = []
    for p in pairs or []:
        key = from_legacy(p.get("category"), p.get("subcategory"))
        if key:
            out.append(key)
        else:
            covered = set(legacy_search(p.get("category") or ""))
            out.extend(k for k in keep or [] if k in covered)
    return list(dict.fromkeys(out))


def pairs_from_specialties(keys: list[str]) -> list[dict]:
    """The old-style pairs older clients read, one per specialty."""
    pairs = [legacy_pair(k) for k in keys]
    return [{"category": c, "subcategory": s} for c, s in dict.fromkeys(pairs)]


def picks_needed(pairs: list[dict] | None, specialties: list[str] | None) -> list[dict]:
    """Categories a vendor sells under that don't say which service, and
    that none of their specialties covers yet — what the "Pick your
    services" prompt asks about, with the services to choose from."""
    have = set(specialties or [])
    out = []
    for cat in dict.fromkeys(p.get("category") for p in pairs or []):
        if not cat or cat == "other":
            continue
        rule = LEGACY_CATEGORIES.get(cat)
        if not rule or rule["service"] is not None:
            continue
        sub_keys = [from_legacy(cat, p.get("subcategory")) for p in pairs if p.get("category") == cat]
        if any(sub_keys):
            continue
        choices = legacy_search(cat)
        if not have & set(choices):
            out.append({"category": cat, "choices": choices})
    return out


def package_service(category: str | None, subcategory: str | None, specialties: list[str] | None) -> str | None:
    """The service a package saved the old way means: its own pair, or —
    when its category alone is ambiguous — the vendor's one specialty in
    that category, if they have exactly one."""
    key = from_legacy(category, subcategory)
    if key or not category:
        return key
    inside = [k for k in specialties or [] if k in set(legacy_search(category))]
    return inside[0] if len(inside) == 1 else None


def clean_attributes(service_key: str | None, values: dict | None) -> dict | None:
    """A package's attributes, checked against its service: only that
    service's attributes, options by key, numbers and yes/no as such."""
    if values is None:
        return None
    if not service_key or service_key not in _SERVICES:
        raise TaxonomyError("Pick the package's service before its details")
    allowed = set(_SERVICES[service_key]["attributes"])
    out: dict = {}
    for key, value in values.items():
        if key not in allowed:
            raise TaxonomyError(f"'{key}' isn't a detail of {_SERVICES[service_key]['label']}")
        spec = ATTRIBUTES[key]
        kind = spec["type"]
        options = {_slug(o) for o in spec.get("options", [])}
        if value is None:
            continue
        if kind == "bool":
            if not isinstance(value, bool):
                raise TaxonomyError(f"{spec['label']} is yes or no")
        elif kind == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise TaxonomyError(f"{spec['label']} is a number")
        elif kind == "single":
            if value not in options:
                raise TaxonomyError(f"Unknown {spec['label']}: {value}")
        else:
            if not isinstance(value, list) or any(v not in options for v in value):
                raise TaxonomyError(f"Unknown {spec['label']}: {value}")
            value = list(dict.fromkeys(value))
        out[key] = value
    return out
