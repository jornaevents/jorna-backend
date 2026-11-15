"""The bundle builder prefers a package whose vibe the client picked (0086's
services.vibe; client redesign's Suggest vendors)."""

from types import SimpleNamespace

from app.services.chatbot_service import VIBE_BONUS, _score_vendor, _vibe_bonus
from app.models.chatbot_schemas import BudgetTier


def _service(vibe):
    return SimpleNamespace(vibe=vibe)


def test_a_matching_vibe_earns_the_bonus_whatever_its_case():
    assert _vibe_bonus(_service("playful"), ["Playful", "modern"]) == VIBE_BONUS
    assert _vibe_bonus(_service("elegant"), ["elegant"]) == VIBE_BONUS


def test_no_vibe_no_style_or_a_different_one_earns_nothing():
    assert _vibe_bonus(_service(None), ["elegant"]) == 0
    assert _vibe_bonus(_service("minimal"), []) == 0
    assert _vibe_bonus(_service("minimal"), ["traditional"]) == 0


def test_the_bonus_breaks_a_tie_but_does_not_beat_a_much_better_rating():
    vendor = lambda rating: SimpleNamespace(rating=rating, tags=[], instagram_tags=[], num_events=0)  # noqa: E731
    score = lambda rating: _score_vendor(vendor(rating), None, ["playful"], [], BudgetTier.MID_RANGE)  # noqa: E731
    matching, plain = _service("playful"), _service(None)
    assert score(4.5) + _vibe_bonus(matching, ["playful"]) > score(4.5) + _vibe_bonus(plain, ["playful"])
    assert score(3.5) + _vibe_bonus(matching, ["playful"]) < score(4.8) + _vibe_bonus(plain, ["playful"])
