import pytest

from conftest import add_app
from pdt import console, deploy_aws, deploy_azure, deploy_azure_container_apps, deploy_common
from pdt import deploy_google_cloud, regions
from pdt.config import validate_app
from pdt.deploy_common import CostEstimate

ECB = b"""<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<Cube><Cube time='2026-10-08'>
<Cube currency='USD' rate='1.25'/><Cube currency='GBP' rate='0.75'/>
</Cube></Cube></gesmes:Envelope>"""


def no_ecb():
    raise OSError("offline")


@pytest.mark.parametrize("name,expected", [
    ("en_GB.UTF-8", "GBP"),
    ("de_DE", "EUR"),
    ("en_GB@currency=EUR", "EUR"),
    ("en-JP", "JPY"),
    ("C", ""),
    ("", ""),
])
def test_a_locale_name_gives_its_currency(name, expected):
    assert regions.locale_currency(name) == expected


@pytest.mark.skipif(regions.os.name == "nt", reason="Windows reads the user locale")
def test_the_currency_falls_back_to_usd(monkeypatch):
    monkeypatch.setattr(regions, "mac_locale", lambda: "")
    monkeypatch.setattr(regions, "posix_currency", lambda: "")
    for name in ("LC_ALL", "LC_MONETARY", "LANG"):
        monkeypatch.delenv(name, raising=False)
    assert regions.local_currency() == "USD"


@pytest.mark.skipif(regions.os.name == "nt", reason="Windows reads the user locale")
def test_the_currency_follows_the_regional_setting(monkeypatch):
    monkeypatch.setattr(regions, "mac_locale", lambda: "en_GB")
    monkeypatch.setattr(regions, "posix_currency", lambda: "")
    monkeypatch.setenv("LC_ALL", "en_GB.UTF-8")
    assert regions.local_currency() == "GBP"


def test_the_platform_currency_key_is_gone(project):
    (project / "pdt.yml").write_text("platform:\n  provider: azure\n  currency: GBP\n")
    add_app(project, "my-report", "schedule: daily\n")
    assert any("currency" in problem for problem in validate_app("my-report"))


@pytest.mark.parametrize("currency,prefix", [
    ("GBP", "£"), ("PLN", "zł"), ("USD", "$USD"), ("CAD", "$CAD"), ("JPY", "¥JPY"),
    ("SEK", "krSEK"), ("CHF", "CHF "),
])
def test_the_currency_prefix(currency, prefix):
    assert console.currency_prefix(currency) == prefix


def test_the_estimate_shows_the_currency_symbol(capsys):
    CostEstimate([("job", 1.5)], "uksouth list prices", currency="GBP").show()
    out = capsys.readouterr().out
    assert "£   1.50" in out and "$" not in out


def test_a_currency_with_no_symbol_shows_its_code(capsys):
    CostEstimate([("job", 1.5)], "switzerlandnorth list prices", currency="CHF").show()
    assert "CHF    1.50" in capsys.readouterr().out


def test_the_ecb_rate_crosses_through_the_euro(monkeypatch):
    monkeypatch.setattr(deploy_common, "ecb_xml", lambda: ECB)
    assert deploy_common.ecb_rate("GBP") == (0.6, "2026-10-08")
    assert deploy_common.ecb_rate("EUR") == (0.8, "2026-10-08")
    with pytest.raises(LookupError):
        deploy_common.ecb_rate("XYZ")


def test_the_cloud_rate_comes_before_the_ecb_rate(monkeypatch):
    monkeypatch.setattr(deploy_common, "ecb_xml", lambda: pytest.fail("fetched"))
    items, currency, note = deploy_common.convert_from_usd(
        [("job", 2.0)], "GBP", ("Azure's rate", lambda: 0.5))
    assert (items, currency, note) == ([("job", 1.0)], "GBP", ", converted from USD at Azure's rate")


def test_a_failed_cloud_rate_falls_back_to_the_ecb_rate(monkeypatch):
    monkeypatch.setattr(deploy_common, "ecb_xml", lambda: ECB)
    items, currency, note = deploy_common.convert_from_usd(
        [("job", 2.0)], "GBP", ("Azure's rate", no_ecb))
    assert currency == "GBP" and items[0][1] == pytest.approx(1.2)
    assert note == ", converted from USD at the ECB rate of 2026-10-08"


def test_with_no_rate_the_estimate_stays_in_usd_and_says_why(monkeypatch):
    monkeypatch.setattr(deploy_common, "ecb_xml", no_ecb)
    items, currency, note = deploy_common.convert_from_usd([("job", 2.0)], "GBP")
    assert (items, currency) == ([("job", 2.0)], "USD")
    assert note.startswith(" in USD, because no USD to GBP rate could be read")


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


def azure_estimate(monkeypatch, gbp_prices: bool):
    def retail_price(region, service, meter, sku, product="", currency="USD"):
        if currency != "USD" and not gbp_prices:
            raise LookupError("no GBP price")
        return (0.5 if currency == "GBP" else 1.0), ""

    monkeypatch.setattr(deploy_azure_container_apps, "retail_price", retail_price)
    monkeypatch.setattr(deploy_azure, "retail_price", retail_price)
    return deploy_azure_container_apps.cost_estimate_for(
        "uksouth", "GBP", "0 6 * * *", "job", "rg", False, 0, None)


def test_azure_takes_prices_in_the_local_currency(monkeypatch):
    estimate = azure_estimate(monkeypatch, gbp_prices=True)
    assert estimate.currency == "GBP"
    assert "in GBP from the Azure Retail Prices API" in estimate.prices


def test_azure_falls_back_to_its_exchange_rate(monkeypatch):
    monkeypatch.setattr(deploy_azure_container_apps, "exchange_rate", lambda currency: 0.5)
    estimate = azure_estimate(monkeypatch, gbp_prices=False)
    assert estimate.currency == "GBP"
    assert "converted from USD at Azure's rate" in estimate.prices


def test_azure_falls_back_to_the_ecb_rate(monkeypatch):
    monkeypatch.setattr(deploy_azure_container_apps, "exchange_rate", lambda currency: no_ecb())
    monkeypatch.setattr(deploy_common, "ecb_xml", lambda: ECB)
    estimate = azure_estimate(monkeypatch, gbp_prices=False)
    assert estimate.currency == "GBP"
    assert "at the ECB rate of 2026-10-08" in estimate.prices


def test_google_cloud_asks_the_catalog_for_the_currency(monkeypatch):
    requests = []

    def fetch_json(request, timeout):
        requests.append(request.full_url)
        return {"skus": []}

    monkeypatch.setattr(deploy_google_cloud, "run_quiet", lambda *args: "token")
    monkeypatch.setattr(deploy_google_cloud, "fetch_json", fetch_json)
    deploy_google_cloud.billing_list("services/X/skus", "skus", "my-project", "GBP")
    assert "currencyCode=GBP" in requests[0]


def google_skus(currency):
    price = {"units": "0", "nanos": 1000}
    return [{"description": description, "category": {"usageType": "OnDemand"},
             "serviceRegions": ["global"],
             "pricingInfo": [{"pricingExpression": {"usageUnit": "mo",
                                                    "tieredRates": [{"unitPrice": price}]}}]}
            for description in ("Jobs CPU", "Jobs Memory", "Job")]


def test_google_cloud_falls_back_to_the_ecb_rate(monkeypatch):
    def billing_list(path, key, project, currency=""):
        if path == "services":
            return [{"displayName": name, "serviceId": name}
                    for name in ("Cloud Run", "Cloud Scheduler", "Secret Manager")]
        if currency not in ("", "USD"):
            raise LookupError("no GBP prices")
        return google_skus(currency)

    monkeypatch.setattr(deploy_google_cloud, "billing_list", billing_list)
    monkeypatch.setattr(deploy_common, "ecb_xml", lambda: ECB)
    estimate = deploy_google_cloud.cost_estimate(
        "my-project", "europe-west2", "0 6 * * *", "job", False, 0, True, currency="GBP")
    assert estimate.currency == "GBP"
    assert "converted from USD at the ECB rate of 2026-10-08" in estimate.prices


def test_aws_converts_at_the_ecb_rate(monkeypatch):
    monkeypatch.setattr(deploy_common, "ecb_xml", lambda: ECB)
    estimate = deploy_aws.cost_estimate("eu-west-2", [("job", 1.0)], "", "GBP")
    assert estimate.currency == "GBP" and estimate.items[0][1] == pytest.approx(0.6)
    assert "converted from USD at the ECB rate of 2026-10-08" in estimate.prices


def test_aws_keeps_usd_and_says_why_with_no_rate(monkeypatch):
    monkeypatch.setattr(deploy_common, "ecb_xml", no_ecb)
    estimate = deploy_aws.cost_estimate("eu-west-2", [("job", 1.0)], "", "GBP")
    assert estimate.currency == "USD"
    assert "in USD, because no USD to GBP rate could be read" in estimate.prices


def test_aws_says_nothing_extra_for_a_usd_user():
    estimate = deploy_aws.cost_estimate("us-east-1", [("job", 1.0)], "", "USD")
    assert estimate.prices == "us-east-1 list prices, before free tiers"
