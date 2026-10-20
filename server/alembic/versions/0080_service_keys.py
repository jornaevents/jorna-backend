"""Vendors and packages say which service they are (docs/DECISIONS.md #35)

Revision ID: 0080_service_keys
Revises: 0079_awaiting_vendor_status
Create Date: 2026-10-20

- vendors.specialties: the service keys a vendor sells (JSON list).
- services.service_key: the one service a package is (indexed), and
  services.attributes: its filters ({attribute: value}).

Backfill from today's category/subcategory, with the taxonomy's mapping as it
stands now (frozen below, so this migration means the same thing later):
- a vendor's specialties from their specializations (or category/subcategory
  when they have none), plus the service of each package they already sell;
- a package's service from its own pair, else — when its category alone is
  ambiguous (Beauty, Planning, …) — the vendor's one specialty in it.
Whatever stays ambiguous stays null, and the vendor is asked to pick
(taxonomy.picks_needed). The category/subcategory columns are untouched.
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0080_service_keys"
down_revision = "0079_awaiting_vendor_status"
branch_labels = None
depends_on = None

# "category|subcategory" → service key.
PAIRS = {'attire|': 'attire',
 'attire|bridal': 'attire',
 'attire|bridal_party': 'attire',
 'attire|general': 'attire',
 'attire|groom': 'attire',
 'attire|groomsmen': 'attire',
 'bar_beverage|': 'bar_beverage',
 'beauty|bridal_makeup': 'bridal_makeup',
 'beauty|hair_stylist': 'hair_stylist',
 'beauty|mehndi_artist': 'mehndi_artist',
 'beauty|nail_artist': 'nail_artist',
 'cakes_desserts|general_desserts': 'general_desserts',
 'cakes_desserts|wedding_cake': 'wedding_cake',
 'catering|': 'catering',
 'cultural_services|baraat_dancers': 'baraat_dancers',
 'cultural_services|bhangra_troupe': 'bhangra_troupe',
 'cultural_services|doli_palki': 'doli_palki',
 'cultural_services|fire_act': 'fire_act',
 'cultural_services|giddha_troupe': 'giddha_troupe',
 'cultural_services|turban_tying': 'turban_tying',
 'favors_gifts|': 'favors_gifts',
 'floral_decor|decor': 'decor',
 'floral_decor|floral': 'floral',
 'guest_hospitality|': 'guest_hospitality',
 'jewelry|': 'jewelry',
 'lighting_av|': 'lighting_av',
 'music_entertainment|bansuri': 'bansuri',
 'music_entertainment|brass_band': 'brass_band',
 'music_entertainment|dhol': 'dhol',
 'music_entertainment|dj': 'dj',
 'music_entertainment|harmonium': 'harmonium',
 'music_entertainment|live_band': 'live_band',
 'music_entertainment|mc': 'mc',
 'music_entertainment|sarangi': 'sarangi',
 'music_entertainment|shehnai': 'shehnai',
 'music_entertainment|sitar': 'sitar',
 'music_entertainment|tabla': 'tabla',
 'music_entertainment|veena': 'veena',
 'music_entertainment|vocalist': 'vocalist',
 'officiants|christian_officiant': 'christian_officiant',
 'officiants|civil_officiant': 'civil_officiant',
 'officiants|hindu_pandit': 'hindu_pandit',
 'officiants|muslim_qazi': 'muslim_qazi',
 'officiants|sikh_granthi': 'sikh_granthi',
 'other|': 'other',
 'photography|': 'photography',
 'rentals|': 'rentals',
 'stationery|': 'stationery',
 'transportation|': 'transportation',
 'venue|': 'venue',
 'videography|': 'videography'}

# A category saved alone → its service, or None when ambiguous.
CATEGORY_SERVICE = {'attire': 'attire',
 'bar_beverage': 'bar_beverage',
 'beauty': None,
 'cakes_desserts': None,
 'catering': 'catering',
 'cultural_services': None,
 'favors_gifts': 'favors_gifts',
 'floral_decor': None,
 'guest_hospitality': 'guest_hospitality',
 'jewelry': 'jewelry',
 'lighting_av': 'lighting_av',
 'music_entertainment': None,
 'officiants': None,
 'other': 'other',
 'photography': 'photography',
 'planning': None,
 'post_wedding': None,
 'rentals': 'rentals',
 'stationery': 'stationery',
 'transportation': 'transportation',
 'venue': 'venue',
 'videography': 'videography'}

# The services each category covers.
CATEGORY_SEARCH = {'attire': ['attire'],
 'bar_beverage': ['bar_beverage'],
 'beauty': ['bridal_makeup', 'hair_stylist', 'mehndi_artist', 'nail_artist', 'saree_draping'],
 'cakes_desserts': ['wedding_cake', 'general_desserts', 'mithai'],
 'catering': ['catering'],
 'cultural_services': ['bhangra_troupe',
                       'giddha_troupe',
                       'baraat_dancers',
                       'fire_act',
                       'turban_tying',
                       'doli_palki'],
 'favors_gifts': ['favors_gifts'],
 'floral_decor': ['floral', 'decor', 'mandap'],
 'guest_hospitality': ['guest_hospitality'],
 'jewelry': ['jewelry'],
 'lighting_av': ['lighting_av'],
 'music_entertainment': ['dhol',
                         'live_band',
                         'brass_band',
                         'vocalist',
                         'qawwali',
                         'tabla',
                         'sitar',
                         'veena',
                         'bansuri',
                         'shehnai',
                         'harmonium',
                         'sarangi',
                         'nadaswaram',
                         'dj',
                         'mc'],
 'officiants': ['hindu_pandit',
                'muslim_qazi',
                'sikh_granthi',
                'christian_officiant',
                'civil_officiant',
                'kirtan'],
 'other': ['other'],
 'photography': ['photography'],
 'planning': ['planner', 'day_of_coordinator'],
 'post_wedding': ['album_design', 'honeymoon_travel'],
 'rentals': ['rentals'],
 'stationery': ['stationery'],
 'transportation': ['transportation'],
 'venue': ['venue'],
 'videography': ['videography']}


def _service(category, subcategory):
    if not category:
        return None
    return PAIRS.get(f"{category}|{subcategory or ''}") or CATEGORY_SERVICE.get(category)


def _load(value):
    if value is None or isinstance(value, list):
        return value or []
    try:
        return json.loads(value) or []
    except (TypeError, ValueError):
        return []


def upgrade():
    op.add_column("vendors", sa.Column("specialties", sa.JSON(), nullable=True))
    op.add_column("services", sa.Column("service_key", sa.String(50), nullable=True))
    op.add_column("services", sa.Column("attributes", sa.JSON(), nullable=True))
    op.create_index("ix_services_service_key", "services", ["service_key"])

    bind = op.get_bind()
    vendors = bind.execute(sa.text("SELECT vendor_id, category, subcategory, specializations FROM vendors")).fetchall()
    specialties = {}
    for vendor_id, category, subcategory, specializations in vendors:
        pairs = _load(specializations) or ([{"category": category, "subcategory": subcategory}]
                                           if category and category != "other" else [])
        keys = [_service(p.get("category"), p.get("subcategory")) for p in pairs]
        specialties[vendor_id] = list(dict.fromkeys(k for k in keys if k))

    # A package with no category of its own is its vendor's (as search reads it).
    vendor_pair = {vendor_id: (category, subcategory) for vendor_id, category, subcategory, _ in vendors}
    services = []
    for service_id, vendor_id, category, subcategory in bind.execute(
        sa.text("SELECT service_id, vendor_id, category, subcategory FROM services ORDER BY service_id")
    ).fetchall():
        if not category:
            category, subcategory = vendor_pair.get(vendor_id, (None, None))
        services.append((service_id, vendor_id, category, subcategory))

    def _set(service_id, vendor_id, key):
        bind.execute(sa.text("UPDATE services SET service_key = :k WHERE service_id = :s"), {"k": key, "s": service_id})
        if vendor_id in specialties and key not in specialties[vendor_id]:
            specialties[vendor_id].append(key)

    # Exact ones first, so what a vendor's other packages say they sell is
    # known before a vague one leans on it — whatever order rows come in.
    vague = []
    for service_id, vendor_id, category, subcategory in services:
        key = _service(category, subcategory)
        if key:
            _set(service_id, vendor_id, key)
        else:
            vague.append((service_id, vendor_id, category))
    for service_id, vendor_id, category in vague:
        inside = [k for k in specialties.get(vendor_id, []) if k in CATEGORY_SEARCH.get(category, [])]
        if len(inside) == 1:
            _set(service_id, vendor_id, inside[0])

    for vendor_id, keys in specialties.items():
        if keys:
            bind.execute(sa.text("UPDATE vendors SET specialties = :v WHERE vendor_id = :id"),
                         {"v": json.dumps(keys), "id": vendor_id})


def downgrade():
    op.drop_index("ix_services_service_key", table_name="services")
    op.drop_column("services", "attributes")
    op.drop_column("services", "service_key")
    op.drop_column("vendors", "specialties")
