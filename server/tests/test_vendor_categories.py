"""The vendor taxonomy endpoint.

Category/subcategory pairs are validated server-side, so clients need the
authoritative list to build a vendor form that can't be rejected.
"""

from app.models.schemas import VENDOR_SUBCATEGORIES, VendorCategory
from tests.test_api import client


def _categories():
    resp = client.get("/vendors/categories")
    assert resp.status_code == 200, resp.text
    return resp.json()["categories"]


def test_lists_every_category_with_a_label():
    cats = _categories()
    assert len(cats) == len(list(VendorCategory))
    by_value = {c["value"] for c in cats}
    assert by_value == {c.value for c in VendorCategory}
    # Every category is presentable — no raw enum values leaking into a form.
    for c in cats:
        assert c["label"] and c["label"] != c["value"].replace("_", " ")


def test_subcategories_match_the_validated_taxonomy():
    """What the endpoint offers must be exactly what create_vendor accepts."""
    for c in _categories():
        expected = VENDOR_SUBCATEGORIES.get(c["value"], [])
        assert [s["value"] for s in c["subcategories"]] == expected


def test_subcategories_are_labelled():
    for c in _categories():
        for sub in c["subcategories"]:
            assert sub["label"], f"{sub['value']} has no label"


def test_is_public():
    """Reference data, not user data — usable before signing up as a vendor."""
    assert client.get("/vendors/categories").status_code == 200


def test_not_shadowed_by_the_vendor_id_route():
    """`/vendors/categories` must not be read as a vendor id."""
    body = client.get("/vendors/categories").json()
    assert "categories" in body, "route was shadowed by /vendors/{vendor_id}"
