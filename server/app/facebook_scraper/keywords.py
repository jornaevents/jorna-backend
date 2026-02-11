"""
Centralised keyword registry for Facebook scraping.

Keywords are used to search Facebook Pages and Groups for
South-Asian wedding vendors.  In the future, this will pull
Service.name values from the database automatically.
"""

# ---------- hard-coded keywords (phase 1) ----------

KEYWORDS: list[str] = [
    # DJ
    "Punjabi DJ",
    "Indian Wedding DJ",
    "Desi DJ",
    "South Asian DJ",
    "Bollywood DJ",
    "Bhangra DJ",
    # Catering
    "Indian Catering",
    "Punjabi Caterer",
    "Desi Food Catering",
    "Indian Wedding Food",
    "South Asian Catering",
    "Halal Catering",
    # Mehndi / Henna
    "Mehndi Artist",
    "Bridal Mehndi",
    "Henna Artist",
    "South Asian Mehndi",
    # Dhol
    "Dhol Player",
    "Dholi",
    "Punjabi Dhol",
    "Wedding Dhol",
    # Singers / Music
    "Punjabi Singer",
    "Indian Wedding Singer",
    "Live Wedding Music",
    "Bollywood Singer",
    # Dance
    "Bhangra Team",
    "Bollywood Dance",
    "Punjabi Dancers",
    "Wedding Dance Group",
    # Decor
    "Indian Wedding Decor",
    "Mandap Decor",
    "South Asian Decor",
    "Floral Mandap",
    # Venue
    "Banquet Hall",
    "Indian Wedding Venue",
    "South Asian Wedding Venue",
]


# ---------- future: dynamic keyword generation ----------

def keywords_from_services(service_names: list[str]) -> list[str]:
    """
    Convert stored Service names into search-friendly keywords.

    Example:
        "Mehndi Artist" -> "Mehndi Artist"  (already natural)
        "LiveWeddingMusic" -> "Live Wedding Music"  (expand CamelCase)

    This will be called when the database layer is wired up.
    """
    return list(service_names)
