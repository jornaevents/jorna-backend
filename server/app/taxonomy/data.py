"""The service taxonomy, as data (docs/DECISIONS.md #35).

Three levels: a group is for browsing; a service is what gets booked, and
every picker selects one; attributes are filters on a service. Keys are
unique across groups and services, so one key always means one thing, and
never change once shipped — labels and synonyms can.

Edited from the draft list the user maintains:
https://claude.ai/code/artifact/3ec21a59-77f9-491a-9c84-b18f6e6bc0c6
"""

# ── Groups, in browse order ──────────────────────────────────────────
# bundle: the bundle builder can fill this group as one slot ("some kind of
# entertainment"), which is what the old Cultural Services slot became.

GROUPS: list[dict] = [
    {"key": "venues", "label": "Venues"},
    {"key": "planning", "label": "Planning"},
    {"key": "food_drink", "label": "Food & Drink"},
    {"key": "cakes_sweets", "label": "Cakes & Sweets"},
    {"key": "photo_video", "label": "Photo & Video"},
    {"key": "music", "label": "Music"},
    {"key": "entertainment", "label": "Entertainment", "bundle": True},
    {"key": "decor_florals", "label": "Decor & Florals"},
    {"key": "rentals_production", "label": "Rentals & Production"},
    {"key": "beauty", "label": "Beauty"},
    {"key": "attire_jewelry", "label": "Attire & Jewelry"},
    {"key": "ceremony", "label": "Ceremony"},
    {"key": "arrivals_transport", "label": "Arrivals & Transport"},
    {"key": "stationery_gifts", "label": "Stationery & Gifts"},
    {"key": "guest_services", "label": "Guest Services"},
    {"key": "after_event", "label": "After the Event"},
    # "other" is the service's key.
    {"key": "more", "label": "Other"},
]


def _s(key, group, label, synonyms=(), *, popular=None, bundle=False, attributes=(), legacy=()):
    return {
        "key": key, "group": group, "label": label, "synonyms": list(synonyms),
        "popular": popular, "bundle": bundle, "attributes": list(attributes),
        # The (category, subcategory) pairs stored today that mean exactly this
        # service. A category saved with no subcategory is in LEGACY_CATEGORIES.
        "legacy": [tuple(p) for p in legacy],
    }


LANGUAGES = ("languages",)

# ── Services, by group ───────────────────────────────────────────────

SERVICES: list[dict] = [
    _s("venue", "venues", "Venue", ("banquet hall", "hall", "hotel", "ballroom", "farm", "estate", "lawn"),
       popular=1, bundle=True,
       attributes=("setting", "venue_type", "capacity", "catering_policy", "alcohol_allowed"),
       legacy=[("venue", None)]),

    _s("planner", "planning", "Wedding planner", ("planner", "full service", "event planner", "coordinator"),
       attributes=("destination",)),
    _s("day_of_coordinator", "planning", "Day-of coordinator", ("day of", "month of", "coordinator")),

    _s("catering", "food_drink", "Caterer", ("catering", "food", "buffet", "chef", "tiffin"),
       popular=2, bundle=True, attributes=("cuisine", "dietary", "service_style"), legacy=[("catering", None)]),
    _s("bar_beverage", "food_drink", "Bar & bartending",
       ("bar", "bartender", "open bar", "drinks", "cocktails", "alcohol"),
       bundle=True, attributes=("bar_type", "licensed"), legacy=[("bar_beverage", None)]),
    _s("beverage_station", "food_drink", "Chai, coffee & paan stations",
       ("chai", "chai cart", "coffee", "paan", "mocktails", "juice", "kulfi cart"), attributes=("station",)),
    _s("food_truck", "food_drink", "Food truck", ("food truck", "chaat cart", "street food"),
       attributes=("cuisine", "dietary")),

    _s("wedding_cake", "cakes_sweets", "Wedding cake", ("cake", "cake baker", "tiered cake"),
       attributes=("dietary",), legacy=[("cakes_desserts", "wedding_cake")]),
    _s("general_desserts", "cakes_sweets", "Desserts & dessert tables",
       ("desserts", "dessert table", "cupcakes", "sweets table"),
       attributes=("dietary",), legacy=[("cakes_desserts", "general_desserts")]),
    _s("mithai", "cakes_sweets", "Mithai", ("mithai", "Indian sweets", "mithai boxes", "halwai"),
       attributes=("dietary",)),

    _s("photography", "photo_video", "Photographer", ("photographer", "photos", "candid", "pre-wedding shoot"),
       popular=4, bundle=True, attributes=("style", "extras"), legacy=[("photography", None)]),
    _s("videography", "photo_video", "Videographer", ("videographer", "video", "cinematographer", "wedding film"),
       popular=9, bundle=True, attributes=("style", "extras"), legacy=[("videography", None)]),
    _s("photo_booth", "photo_video", "Photo booth", ("photo booth", "360 booth", "selfie booth", "GIF booth")),
    _s("content_creator", "photo_video", "Content creator",
       ("content creator", "reels", "social media", "same-day content")),

    _s("dhol", "music", "Dhol player", ("dhol", "dholi", "dhol wala", "dhol player"),
       popular=7, bundle=True, legacy=[("music_entertainment", "dhol")]),
    _s("live_band", "music", "Live band", ("band", "live music", "cover band"),
       attributes=("music_style",), legacy=[("music_entertainment", "live_band")]),
    _s("brass_band", "music", "Baraat band", ("baraat band", "brass band", "band baja"),
       legacy=[("music_entertainment", "brass_band")]),
    _s("vocalist", "music", "Singer", ("singer", "vocalist", "ghazal", "sangeet singer"),
       attributes=("music_style", *LANGUAGES), legacy=[("music_entertainment", "vocalist")]),
    _s("qawwali", "music", "Qawwali group", ("qawwali", "qawwal", "sufi")),
    _s("tabla", "music", "Tabla player", ("tabla",), legacy=[("music_entertainment", "tabla")]),
    _s("sitar", "music", "Sitar player", ("sitar",), legacy=[("music_entertainment", "sitar")]),
    _s("veena", "music", "Veena player", ("veena", "vina"), legacy=[("music_entertainment", "veena")]),
    _s("bansuri", "music", "Bansuri / flute player", ("bansuri", "flute"),
       legacy=[("music_entertainment", "bansuri")]),
    _s("shehnai", "music", "Shehnai player", ("shehnai", "shenai"), legacy=[("music_entertainment", "shehnai")]),
    _s("harmonium", "music", "Harmonium player", ("harmonium", "baja"),
       legacy=[("music_entertainment", "harmonium")]),
    _s("sarangi", "music", "Sarangi player", ("sarangi",), legacy=[("music_entertainment", "sarangi")]),
    _s("nadaswaram", "music", "Nadaswaram & thavil",
       ("nadaswaram", "nagaswaram", "thavil", "South Indian band")),

    _s("dj", "entertainment", "DJ", ("DJ", "disc jockey", "sound"),
       popular=5, bundle=True, attributes=("music_style", "dj_includes"), legacy=[("music_entertainment", "dj")]),
    _s("mc", "entertainment", "MC / emcee", ("MC", "emcee", "host", "anchor"),
       popular=12, attributes=LANGUAGES, legacy=[("music_entertainment", "mc")]),
    _s("dance_choreographer", "entertainment", "Dance choreographer",
       ("choreographer", "sangeet dance", "dance teacher", "first dance"), popular=11),
    _s("bhangra_troupe", "entertainment", "Bhangra troupe", ("bhangra", "bhangra team"),
       legacy=[("cultural_services", "bhangra_troupe")]),
    _s("giddha_troupe", "entertainment", "Giddha troupe", ("giddha", "gidda"),
       legacy=[("cultural_services", "giddha_troupe")]),
    _s("baraat_dancers", "entertainment", "Baraat dancers", ("baraat dancers", "nachne wale"),
       legacy=[("cultural_services", "baraat_dancers")]),
    _s("garba_dandiya", "entertainment", "Garba / dandiya leader", ("garba", "dandiya", "raas")),
    _s("fire_act", "entertainment", "Fire & specialty acts",
       ("fire dancer", "LED dancers", "acrobats", "performers"), legacy=[("cultural_services", "fire_act")]),
    _s("kids_entertainment", "entertainment", "Kids' entertainment",
       ("magician", "face painting", "balloon artist", "kids")),

    _s("floral", "decor_florals", "Florist", ("florist", "flowers", "garlands", "varmala", "phoolon"),
       popular=8, bundle=True, attributes=("florals",), legacy=[("floral_decor", "floral")]),
    _s("decor", "decor_florals", "Decorator",
       ("decor", "decorator", "stage", "backdrop", "centerpieces", "draping"),
       popular=3, bundle=True, attributes=("decor_specialty",), legacy=[("floral_decor", "decor")]),
    _s("mandap", "decor_florals", "Mandap", ("mandap", "mandap rental", "chuppah", "canopy")),

    _s("rentals", "rentals_production", "Event rentals",
       ("rentals", "furniture", "chairs", "linens", "lounge", "tableware"),
       attributes=("rental_items",), legacy=[("rentals", None)]),
    _s("tent", "rentals_production", "Tents & structures", ("tent", "marquee", "shamiana", "canopy")),
    _s("lighting_av", "rentals_production", "Lighting & sound",
       ("lighting", "uplighting", "AV", "sound system", "LED wall", "stage"),
       attributes=("av_services",), legacy=[("lighting_av", None)]),
    _s("special_effects", "rentals_production", "Special effects",
       ("cold pyro", "sparklers", "fog", "confetti", "grand entrance")),

    _s("bridal_makeup", "beauty", "Makeup artist", ("makeup", "MUA", "bridal makeup", "airbrush"),
       popular=10, bundle=True, attributes=("beauty_clients", "makeup_technique", "on_site"),
       legacy=[("beauty", "bridal_makeup")]),
    _s("hair_stylist", "beauty", "Hair stylist", ("hair", "hairstylist", "bridal hair"),
       attributes=("beauty_clients", "on_site"), legacy=[("beauty", "hair_stylist")]),
    _s("mehndi_artist", "beauty", "Mehndi artist", ("mehndi", "mehendi", "henna"),
       popular=6, bundle=True, attributes=("mehndi_type", "on_site"), legacy=[("beauty", "mehndi_artist")]),
    _s("nail_artist", "beauty", "Nail artist", ("nails", "manicure"),
       attributes=("on_site",), legacy=[("beauty", "nail_artist")]),
    _s("saree_draping", "beauty", "Saree & dupatta draping", ("saree draping", "dupatta setting", "draping"),
       attributes=("on_site",)),

    # Today's attire subcategories (bridal, groom, …) become its "Who it's for".
    _s("attire", "attire_jewelry", "Outfits & designers",
       ("lehenga", "sherwani", "outfit", "designer", "boutique"), attributes=("attire_for", "offering"),
       legacy=[("attire", None), ("attire", "bridal"), ("attire", "groom"), ("attire", "bridal_party"),
               ("attire", "groomsmen"), ("attire", "general")]),
    _s("tailoring", "attire_jewelry", "Tailoring & alterations", ("tailor", "alterations", "darzi")),
    _s("jewelry", "attire_jewelry", "Jewelry", ("jewellery", "jewelry", "bridal set", "kundan", "polki"),
       attributes=("offering",), legacy=[("jewelry", None)]),
    _s("turban_tying", "attire_jewelry", "Turban & safa tying", ("turban", "safa", "pagri", "pagdi"),
       legacy=[("cultural_services", "turban_tying")]),

    _s("hindu_pandit", "ceremony", "Hindu pandit / priest", ("pandit", "pandit ji", "priest", "purohit", "shastri"),
       attributes=LANGUAGES, legacy=[("officiants", "hindu_pandit")]),
    _s("muslim_qazi", "ceremony", "Muslim qazi / imam", ("qazi", "kazi", "imam", "nikah"),
       attributes=LANGUAGES, legacy=[("officiants", "muslim_qazi")]),
    _s("sikh_granthi", "ceremony", "Sikh granthi", ("granthi", "anand karaj", "gurdwara"),
       attributes=LANGUAGES, legacy=[("officiants", "sikh_granthi")]),
    _s("christian_officiant", "ceremony", "Christian officiant", ("pastor", "priest", "minister", "church"),
       attributes=LANGUAGES, legacy=[("officiants", "christian_officiant")]),
    _s("civil_officiant", "ceremony", "Civil / non-denominational officiant",
       ("officiant", "civil", "interfaith", "celebrant"),
       attributes=LANGUAGES, legacy=[("officiants", "civil_officiant")]),
    _s("kirtan", "ceremony", "Kirtan & bhajan group", ("kirtan", "shabad", "bhajan", "ragi jatha"),
       attributes=LANGUAGES),

    _s("baraat_horse", "arrivals_transport", "Baraat horse", ("ghodi", "horse", "baraat horse")),
    _s("doli_palki", "arrivals_transport", "Doli / palki", ("doli", "palki", "palanquin"),
       legacy=[("cultural_services", "doli_palki")]),
    _s("transportation", "arrivals_transport", "Cars & limos",
       ("limo", "vintage car", "getaway car", "Rolls Royce"), attributes=("vehicle",),
       legacy=[("transportation", None)]),
    _s("shuttle", "arrivals_transport", "Guest shuttles", ("shuttle", "bus", "coach")),

    _s("stationery", "stationery_gifts", "Invitations & stationery",
       ("invitations", "cards", "e-invite", "save the date"),
       attributes=("stationery_format",), legacy=[("stationery", None)]),
    _s("signage", "stationery_gifts", "Signage", ("welcome sign", "seating chart", "neon sign", "boards")),
    _s("favors_gifts", "stationery_gifts", "Favors & return gifts",
       ("favors", "return gifts", "mithai boxes", "welcome bags"), legacy=[("favors_gifts", None)]),

    _s("guest_hospitality", "guest_services", "Hospitality & welcome desk", ("hospitality", "welcome desk", "ushers"),
       legacy=[("guest_hospitality", None)]),
    _s("hotel_block", "guest_services", "Hotel room blocks", ("hotel block", "room block", "guest rooms")),
    _s("childcare", "guest_services", "Childcare", ("babysitter", "kids room", "nanny")),
    _s("valet", "guest_services", "Valet", ("valet", "parking")),
    _s("security", "guest_services", "Security", ("security", "guards", "bouncer")),

    _s("album_design", "after_event", "Album design", ("album", "photo book")),
    _s("honeymoon_travel", "after_event", "Honeymoon travel", ("honeymoon", "travel agent")),

    _s("other", "more", "Other", legacy=[("other", None)]),
]


# ── Attributes ───────────────────────────────────────────────────────
# type: single | multi | number | bool. Options are labels; their keys are
# slugs of the label (taxonomy._slug), stable as long as the label is.

ATTRIBUTES: dict[str, dict] = {
    "setting": {"label": "Setting", "type": "single", "options": ["Indoor", "Outdoor", "Both"]},
    "venue_type": {"label": "Venue type", "type": "multi", "options": [
        "Banquet hall", "Hotel", "Estate / farm", "Restaurant", "Community hall", "Temple / gurdwara",
        "Backyard / private home"]},
    "capacity": {"label": "Guest capacity", "type": "number", "unit": "seated guests"},
    "catering_policy": {"label": "Catering policy", "type": "single", "options": [
        "In-house only", "Outside caterers allowed", "Either"]},
    "alcohol_allowed": {"label": "Alcohol allowed", "type": "bool"},
    "destination": {"label": "Destination weddings", "type": "bool"},
    "cuisine": {"label": "Cuisine", "type": "multi", "options": [
        "North Indian", "Punjabi", "Gujarati", "South Indian", "Bengali", "Hyderabadi", "Pakistani",
        "Indo-Chinese", "Continental", "Fusion"]},
    "dietary": {"label": "Dietary", "type": "multi", "options": [
        "Vegetarian", "Vegan", "Jain", "Halal", "Eggless", "Gluten-free"]},
    "service_style": {"label": "Service style", "type": "multi", "options": [
        "Buffet", "Plated", "Family style", "Live stations"]},
    "bar_type": {"label": "Bar type", "type": "multi", "options": ["Full open bar", "Beer & wine", "Mocktails only"]},
    "licensed": {"label": "Licensed to serve", "type": "bool"},
    "station": {"label": "Station", "type": "multi", "options": ["Chai", "Coffee", "Paan", "Mocktails", "Kulfi"]},
    "style": {"label": "Style", "type": "multi", "options": [
        "Candid", "Traditional", "Editorial", "Cinematic", "Documentary"]},
    "extras": {"label": "Extras", "type": "multi", "options": [
        "Drone", "Same-day edit", "Livestream", "Album included", "Second shooter"]},
    "music_style": {"label": "Music", "type": "multi", "options": [
        "Bollywood", "Punjabi", "Western", "Arabic", "Tamil / Telugu", "Classical"]},
    "dj_includes": {"label": "Includes", "type": "multi", "options": ["MC", "Lighting", "Dance floor", "Dhol add-on"]},
    "florals": {"label": "Florals", "type": "multi", "options": [
        "Fresh", "Artificial", "Garlands / varmala", "Car decor"]},
    "decor_specialty": {"label": "Specialty", "type": "multi", "options": [
        "Stage", "Mandap", "Centerpieces", "Ceiling & draping", "Entrance", "Sangeet / mehndi setups"]},
    "rental_items": {"label": "Items", "type": "multi", "options": [
        "Furniture", "Lounge", "Linens", "Tableware", "Dance floor"]},
    "av_services": {"label": "Services", "type": "multi", "options": [
        "Uplighting", "Sound system", "LED wall", "Stage", "Gobo", "Livestream"]},
    "beauty_clients": {"label": "Clients", "type": "multi", "options": ["Bride", "Groom", "Family & guests"]},
    "makeup_technique": {"label": "Technique", "type": "multi", "options": ["Airbrush", "HD", "Traditional"]},
    "mehndi_type": {"label": "Mehndi", "type": "multi", "options": [
        "Bridal", "Guest", "Organic", "Arabic", "Indo-Western"]},
    "on_site": {"label": "Travels to client", "type": "bool"},
    "attire_for": {"label": "Who it's for", "type": "multi", "options": [
        "Bride", "Groom", "Bridal party", "Groomsmen", "Family"]},
    "offering": {"label": "Offering", "type": "multi", "options": ["Custom", "Ready-made", "Rental"]},
    "vehicle": {"label": "Vehicle", "type": "multi", "options": ["Limo", "Vintage car", "Luxury car", "Party bus"]},
    "languages": {"label": "Languages", "type": "multi", "options": [
        "English", "Hindi", "Punjabi", "Urdu", "Gujarati", "Sanskrit", "Tamil", "Telugu", "Bengali", "Marathi"]},
    "stationery_format": {"label": "Format", "type": "multi", "options": [
        "Printed", "Digital / e-invite", "Boxed invitations"]},
}


# ── Today's categories ───────────────────────────────────────────────
# What a category key stored or sent today means in the new taxonomy:
#   search: what an old ?category= link or search filter now covers (group
#           or service keys, expanded by taxonomy.expand).
#   service: the service a vendor or package saved with this category and
#           no subcategory becomes — None means it's ambiguous, and the
#           vendor is asked to pick ("Pick your services").

LEGACY_CATEGORIES: dict[str, dict] = {
    "venue": {"search": ["venue"], "service": "venue"},
    # Decided 2026-10-20: plain "planning" vendors choose planner or day-of.
    "planning": {"search": ["planning"], "service": None},
    "catering": {"search": ["catering"], "service": "catering"},
    "bar_beverage": {"search": ["bar_beverage"], "service": "bar_beverage"},
    "cakes_desserts": {"search": ["cakes_sweets"], "service": None},
    "photography": {"search": ["photography"], "service": "photography"},
    "videography": {"search": ["videography"], "service": "videography"},
    # Music & Entertainment split: its DJs and MCs are Entertainment now.
    "music_entertainment": {"search": ["music", "dj", "mc"], "service": None},
    "floral_decor": {"search": ["floral", "decor", "mandap"], "service": None},
    "rentals": {"search": ["rentals"], "service": "rentals"},
    "lighting_av": {"search": ["lighting_av"], "service": "lighting_av"},
    "beauty": {"search": ["beauty"], "service": None},
    "attire": {"search": ["attire"], "service": "attire"},
    "jewelry": {"search": ["jewelry"], "service": "jewelry"},
    "stationery": {"search": ["stationery"], "service": "stationery"},
    "transportation": {"search": ["transportation"], "service": "transportation"},
    "officiants": {"search": ["ceremony"], "service": None},
    "guest_hospitality": {"search": ["guest_hospitality"], "service": "guest_hospitality"},
    "favors_gifts": {"search": ["favors_gifts"], "service": "favors_gifts"},
    "cultural_services": {
        "search": ["bhangra_troupe", "giddha_troupe", "baraat_dancers", "fire_act", "turban_tying", "doli_palki"],
        "service": None,
    },
    "post_wedding": {"search": ["after_event"], "service": None},
    "other": {"search": ["other"], "service": "other"},
}

# The bundle builder's slot keys today (chatbot_schemas.CHATBOT_SLOTS) and
# what each becomes: a service, or a group filled as one slot.
LEGACY_BUNDLE_SLOTS: dict[str, str] = {
    "venue": "venue",
    "catering": "catering",
    "photography": "photography",
    "videography": "videography",
    "dj": "dj",
    "dhol": "dhol",
    "makeup": "bridal_makeup",
    "mehndi": "mehndi_artist",
    "floral_decor": "decor_florals",
    "cultural_services": "entertainment",
}
