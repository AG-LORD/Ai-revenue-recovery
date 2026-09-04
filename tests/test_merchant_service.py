from app.services.merchant_service import (
    DEMO_MERCHANTS,
    get_merchant_by_key,
    get_merchants,
    init_merchant_registry,
)


def test_demo_merchants_are_seeded():
    init_merchant_registry()
    merchants = get_merchants()

    assert [merchant["merchant_key"] for merchant in merchants] == [
        merchant["merchant_key"] for merchant in DEMO_MERCHANTS
    ]
    assert all(merchant["status"] == "ACTIVE" for merchant in merchants)


def test_merchant_lookup_isolated_by_key():
    urban_cart = get_merchant_by_key("urban_cart")
    fit_gear = get_merchant_by_key("fit_gear")

    assert urban_cart is not None
    assert fit_gear is not None
    assert urban_cart["id"] != fit_gear["id"]
    assert urban_cart["business_name"] == "UrbanCart"
    assert fit_gear["business_name"] == "FitGear"


def test_unknown_merchant_is_rejected():
    assert get_merchant_by_key("does_not_exist") is None
