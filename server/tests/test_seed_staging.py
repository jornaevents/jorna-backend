"""The staging seed's vendors have to be findable by the bundle builder.

The builder's DJ, dhol, makeup and mehndi slots match on subcategory, and the
seed used to create its DJ and mehndi artist without one. On staging that made
the only DJ invisible to the builder ("No available DJ for your date"), which
read as if a pending request were holding the date (backend #83).
"""

from app.models.chatbot_schemas import CHATBOT_SLOTS
from scripts.seed_staging import VENDORS


def _fills_a_slot(category: str, subcategory: str | None) -> bool:
    return any(
        slot["category"] == category and slot["subcategory"] in (None, subcategory)
        for slot in CHATBOT_SLOTS.values()
    )


def test_every_seeded_vendor_fills_a_builder_slot():
    for v in VENDORS:
        assert _fills_a_slot(v["category"], v.get("subcategory")), v["slug"]


def test_seeded_dj_fills_the_dj_slot():
    dj = next(v for v in VENDORS if v["slug"] == "dj")
    slot = CHATBOT_SLOTS["dj"]
    assert (dj["category"], dj.get("subcategory")) == (slot["category"], slot["subcategory"])
