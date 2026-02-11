"""
Centralised hashtag registry.

For now the list is hard-coded.  In the future, it will pull
Service.name values from the database and convert them into
hashtag queries automatically.
"""

# ---------- hard-coded hashtags (phase 1) ----------

HASHTAGS: list[str] = [
    # DJ
    "PunjabiDJ",
    "IndianWeddingDJ",
    "DesiDJ",
    "SouthAsianDJ",
    "BollywoodDJ",
    "BhangraDJ",
    "PunjabiWedding",
    # Catering
    "IndianCatering",
    "PunjabiCaterer",
    "DesiFood",
    "IndianWeddingFood",
    "SouthAsianCatering",
    "HalalCatering",
    # Mehndi / Henna
    "MehndiArtist",
    "BridalMehndi",
    "IndianMehndi",
    "HennaArtist",
    "SouthAsianMehndi",
    # Dhol
    "Dhol",
    "DholPlayer",
    "Dholi",
    "PunjabiDhol",
    "WeddingDhol",
    # Singers / Music
    "PunjabiSinger",
    "IndianWeddingSinger",
    "LiveWeddingMusic",
    "BollywoodSinger",
    # Dance
    "BhangraTeam",
    "BollywoodDance",
    "PunjabiDancers",
    "WeddingDance",
    "SouthAsianDance",
    # Decor
    "IndianWeddingDecor",
    "MandapDecor",
    "SouthAsianDecor",
    "WeddingDecor",
    "FloralMandap",
    # Venue
    "BanquetHall",
    "IndianWeddingVenue",
    "SouthAsianWedding",
    "WeddingVenue",
]


# ---------- future: dynamic hashtag generation ----------

def hashtags_from_services(service_names: list[str]) -> list[str]:
    """
    Convert stored Service names into hashtag-like strings.

    Example:
        "Mehndi Artist" -> "MehndiArtist"
        "Live Wedding Music" -> "LiveWeddingMusic"

    This will be called when the database layer is wired up.
    """
    tags: list[str] = []
    for name in service_names:
        # CamelCase the service name and strip non-alpha chars
        tag = "".join(word.capitalize() for word in name.split())
        tag = "".join(ch for ch in tag if ch.isalnum())
        tags.append(tag)
    return tags
