import pytest

from conftest import add_app
from pdt import deploy_aws, deploy_azure, deploy_google_cloud, regions
from pdt.config import CURRENCIES, validate_app
from pdt.deploy_common import CostEstimate


@pytest.mark.parametrize("zone,expected", [
    ("Europe/London", "GBP"),
    ("Europe/Dublin", "EUR"),
    ("Europe/Zurich", "CHF"),
    ("Asia/Tokyo", "JPY"),
    ("America/New_York", "USD"),
    # Hungary pays in forints, which no price API converts to.
    ("Europe/Budapest", "USD"),
    ("Etc/UTC", "USD"),
    ("", "USD"),
])
def test_currency_follows_the_time_zone(monkeypatch, zone, expected):
    monkeypatch.setattr(regions, "local_timezone", lambda: zone)
    assert regions.local_currency({}) == expected


def test_platform_currency_overrides_the_time_zone(monkeypatch):
    monkeypatch.setattr(regions, "local_timezone", lambda: "Europe/London")
    assert regions.local_currency({"currency": "USD"}) == "USD"


def test_every_local_currency_is_one_the_price_apis_convert_to():
    assert set(regions.COUNTRY_CURRENCY.values()) <= set(CURRENCIES)


def test_an_unsupported_currency_fails_validation(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n  currency: PLN\n")
    add_app(project, "my-report", "schedule: daily\n")
    problems = validate_app("my-report")
    assert any("platform.currency" in problem and "GBP" in problem for problem in problems)


def test_a_supported_currency_passes_validation(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n  currency: GBP\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert not any("currency" in problem for problem in validate_app("my-report"))


def test_the_estimate_shows_the_currency_symbol(capsys):
    CostEstimate([("job", 1.5)], "uksouth list prices", currency="GBP").show()
    out = capsys.readouterr().out
    assert "£   1.50" in out and "$" not in out


def test_a_currency_with_no_symbol_shows_its_code(capsys):
    CostEstimate([("job", 1.5)], "switzerlandnorth list prices", currency="CHF").show()
    assert "CHF    1.50" in capsys.readouterr().out


def test_azure_takes_its_exchange_rate_from_one_meter_in_both_currencies(monkeypatch):
    urls = []

    def fetch_json(url, timeout):
        urls.append(url)
        price = 100.0 if "currencyCode='USD'" in url else 75.0
        return {"Items": [{"retailPrice": price}]}

    monkeypatch.setattr(deploy_azure, "fetch_json", fetch_json)
    assert deploy_azure.exchange_rate("GBP") == 0.75
    assert len(urls) == 2 and all(deploy_azure.RATE_METER in url for url in urls)


def test_azure_needs_no_rate_for_usd(monkeypatch):
    monkeypatch.setattr(deploy_azure, "fetch_json", lambda *args, **kwargs: pytest.fail("fetched"))
    assert deploy_azure.exchange_rate("USD") == 1.0


def test_azure_says_the_prices_are_converted():
    estimate = deploy_azure.cost_estimate("uksouth", [("job", 1.0)], "", "GBP")
    assert estimate.currency == "GBP"
    assert "converted from USD at Azure's rate" in estimate.prices


def test_google_cloud_asks_the_catalog_for_the_currency(monkeypatch):
    requests = []

    def fetch_json(request, timeout):
        requests.append(request.full_url)
        return {"skus": []}

    monkeypatch.setattr(deploy_google_cloud, "run_quiet", lambda *args: "token")
    monkeypatch.setattr(deploy_google_cloud, "fetch_json", fetch_json)
    deploy_google_cloud.billing_list("services/X/skus", "skus", "my-project", "GBP")
    assert "currencyCode=GBP" in requests[0]


def test_aws_keeps_usd_and_says_why():
    estimate = deploy_aws.cost_estimate("eu-west-2", [("job", 1.0)], "", "GBP")
    assert estimate.currency == "USD"
    assert "in USD, the only currency AWS lists" in estimate.prices


def test_aws_says_nothing_extra_for_a_usd_user():
    estimate = deploy_aws.cost_estimate("us-east-1", [("job", 1.0)], "", "USD")
    assert estimate.prices == "us-east-1 list prices, before free tiers"
