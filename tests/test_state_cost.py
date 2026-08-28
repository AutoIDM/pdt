from __future__ import annotations

import pytest

from pdt import deploy_azure, deploy_google_cloud


def sku(description: str, price: float, factor: int = 1) -> dict:
    units, nanos = divmod(int(price * 1_000_000_000), 1_000_000_000)
    return {
        "category": {"usageType": "OnDemand"},
        "serviceRegions": ["us-central1"],
        "description": description,
        "pricingInfo": [{"pricingExpression": {
            "baseUnitConversionFactor": factor,
            "tieredRates": [{"unitPrice": {"units": str(units), "nanos": nanos}}],
        }}],
    }


def test_google_state_cost_uses_one_read_and_write_per_run():
    items = deploy_google_cloud.state_cost_items([
        sku("Standard Storage", 0.02),
        sku("Regional Standard Class A Operations", 0.000005, 1_000),
        sku("Regional Standard Class B Operations", 0.0000004, 10_000),
    ], "us-central1", 30)
    assert items[0][0] == "Cloud Storage state: 1 KiB stored"
    assert items[1] == ("Cloud Storage state: ~30 reads and writes", 0.000162)


def test_azure_state_cost_selects_block_blob_prices(monkeypatch):
    prices = {
        "LRS Data Stored": (0.024, "1 GB/Month"),
        "Read Operations": (0.00036, "10K"),
        "LRS Write Operations": (0.00036, "10K"),
    }

    def price(_region, _service, meter, _sku, product=""):
        assert product == "General Block Blob"
        return prices[meter]

    monkeypatch.setattr(deploy_azure, "retail_price", price)
    items = deploy_azure.state_cost_items("eastus", 30)
    assert items[0][0] == "Blob state: 1 KiB stored"
    assert items[1][0] == "Blob state: ~30 reads and writes"
    assert items[1][1] == pytest.approx(0.00000216)
