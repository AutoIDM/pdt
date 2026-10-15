import datetime
import json
import threading

import pytest

from pdt import deploy_aws, deploy_azure, deploy_common, deploy_google_cloud

DAY = deploy_common.PRICE_MAX_AGE_SECONDS


def reader(value, calls):
    def read():
        calls.append(value)
        return value
    return read


def failing(calls):
    def read():
        calls.append("failed")
        raise OSError("offline")
    return read


def at(monkeypatch, seconds):
    monkeypatch.setattr(deploy_common.time, "time", lambda: seconds)


def test_a_miss_reads_and_a_hit_does_not(price_cache):
    calls = []
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(1.5, calls))[0] == 1.5
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(9.0, calls))[0] == 1.5
    assert calls == [1.5]
    assert len(list(price_cache.glob("*.json"))) == 1


def test_an_entry_older_than_one_day_is_read_again(monkeypatch):
    calls = []
    at(monkeypatch, 1_000_000)
    deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(1.0, calls))
    at(monkeypatch, 1_000_000 + DAY - 1)
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(2.0, calls))[0] == 1.0
    at(monkeypatch, 1_000_000 + DAY + 1)
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(2.0, calls))[0] == 2.0
    assert calls == [1.0, 2.0]


def test_an_entry_from_the_future_is_read_again(monkeypatch):
    calls = []
    at(monkeypatch, 2_000_000)
    deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(1.0, calls))
    at(monkeypatch, 1_000_000)
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(2.0, calls))[0] == 2.0


@pytest.mark.parametrize("other", [
    ("azure", "us-east-1", "USD", "q"),
    ("aws", "eu-west-2", "USD", "q"),
    ("aws", "us-east-1", "GBP", "q"),
    ("aws", "us-east-1", "USD", "q2"),
])
def test_each_key_has_its_own_entry(other):
    calls = []
    deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(1.0, calls))
    assert deploy_common.cached_price(*other, reader(2.0, calls))[0] == 2.0
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(3.0, calls))[0] == 1.0
    assert calls == [1.0, 2.0]


@pytest.mark.parametrize("text", ["", "{", "[]", '{"key": 1}', '{"read_at": "x", "value": 1}'])
def test_a_corrupt_file_is_read_again_and_replaced(price_cache, text):
    calls = []
    deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(1.0, calls))
    path = next(price_cache.glob("*.json"))
    path.write_text(text)
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(2.0, calls))[0] == 2.0
    assert json.loads(path.read_text())["value"] == 2.0


def test_an_entry_for_another_key_in_the_same_file_is_not_used(price_cache):
    calls = []
    deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(1.0, calls))
    path = next(price_cache.glob("*.json"))
    saved = json.loads(path.read_text())
    saved["key"]["query"] = "other"
    path.write_text(json.dumps(saved))
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(2.0, calls))[0] == 2.0


def test_a_failed_read_of_an_old_entry_uses_it_and_names_its_date(monkeypatch, capsys):
    calls = []
    first = datetime.datetime(2026, 10, 13, 12).timestamp()
    at(monkeypatch, first)
    deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(1.0, calls))
    at(monkeypatch, first + 2 * DAY)
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", failing(calls)) == (
        1.0, "2026-10-13")
    out = capsys.readouterr().out
    assert "Could not read current prices (offline)" in out
    assert "the prices pdt read on 2026-10-13" in out


def test_a_failed_read_with_no_entry_raises():
    with pytest.raises(OSError):
        deploy_common.cached_price("aws", "us-east-1", "USD", "q", failing([]))


def test_a_folder_pdt_cannot_write_still_gives_the_price(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setattr(deploy_common, "PRICE_CACHE", blocker / "prices")
    assert deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(1.0, []))[0] == 1.0


def test_concurrent_writes_never_leave_a_partial_file(price_cache):
    value = ["x" * 1000] * 200
    deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(value, []))
    path = next(price_cache.glob("*.json"))
    seen = []
    stop = threading.Event()

    def write():
        for _ in range(50):
            path.unlink(missing_ok=True)
            deploy_common.cached_price("aws", "us-east-1", "USD", "q", reader(value, []))

    def read():
        while not stop.is_set():
            try:
                text = path.read_text()
            except FileNotFoundError:
                continue
            try:
                seen.append(json.loads(text)["value"] == value)
            except ValueError:
                seen.append(False)

    watcher = threading.Thread(target=read)
    watcher.start()
    writers = [threading.Thread(target=write) for _ in range(4)]
    for thread in writers:
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    watcher.join()
    assert seen and all(seen)
    assert [p.name for p in price_cache.iterdir()] == [path.name]


def test_the_estimate_names_the_date_of_its_oldest_price(monkeypatch):
    at(monkeypatch, datetime.datetime(2026, 10, 14, 12).timestamp())
    deploy_common.cached_list_price("aws", "us-east-1", "USD", "a", reader(1.0, []))
    at(monkeypatch, datetime.datetime(2026, 10, 15, 12).timestamp())
    deploy_common.cached_list_price("aws", "us-east-1", "USD", "b", reader(1.0, []))
    estimate = deploy_aws.cost_estimate("us-east-1", [("job", 1.0)], "", "USD")
    assert estimate.prices == "us-east-1 list prices of 2026-10-14, before free tiers"


def test_the_ecb_rate_is_cached_with_its_own_date(monkeypatch):
    from test_currency import ECB
    calls = []
    monkeypatch.setattr(deploy_common, "ecb_xml", lambda: calls.append(1) or ECB)
    assert deploy_common.ecb_rate("GBP") == (0.6, "2026-10-08")
    assert deploy_common.ecb_rate("GBP") == (0.6, "2026-10-08")
    assert calls == [1]
    assert deploy_common.LIST_PRICE_DATES == set()


def test_aws_reads_each_price_file_once(monkeypatch):
    urls = []
    data = {"products": {
        "cpu": {"attributes": {"usagetype": "USE1-Fargate-ARM-vCPU-Hours:perCPU"}},
        "mem": {"attributes": {"usagetype": "USE1-Fargate-ARM-GB-Hours"}}},
        "terms": {"OnDemand": {
            "cpu": {"t": {"priceDimensions": {"d": {"pricePerUnit": {"USD": "0.03"}}}}},
            "mem": {"t": {"priceDimensions": {"d": {"pricePerUnit": {"USD": "0.004"}}}}}}}}

    def fetch_json(url, timeout):
        urls.append(url)
        return data

    monkeypatch.setattr(deploy_aws, "fetch_json", fetch_json)
    deploy_aws.price_list.cache_clear()
    assert deploy_aws.list_price("AmazonECS", "us-east-1", "Fargate-ARM-vCPU-Hours:perCPU") == 0.03
    assert deploy_aws.list_price("AmazonECS", "us-east-1", "Fargate-ARM-GB-Hours") == 0.004
    deploy_aws.price_list.cache_clear()
    assert deploy_aws.list_price("AmazonECS", "us-east-1", "Fargate-ARM-GB-Hours") == 0.004
    assert len(urls) == 1


def test_azure_reads_a_retail_price_once(monkeypatch):
    urls = []

    def fetch_json(url, timeout):
        urls.append(url)
        return {"Items": [{"retailPrice": 0.5, "unitOfMeasure": "1 Hour"}]}

    monkeypatch.setattr(deploy_azure, "fetch_json", fetch_json)
    for _ in range(2):
        assert deploy_azure.retail_price("uksouth", "Container Registry", "Basic Registry Unit",
                                         "Basic", currency="GBP") == (0.5, "1 Hour")
        assert deploy_azure.exchange_rate("GBP")[0] == 1.0
    assert len(urls) == 3


def test_google_cloud_reads_the_catalog_once(monkeypatch):
    from test_currency import google_skus
    paths = []

    def billing_list(path, key, project, currency=""):
        paths.append(path)
        if path == "services":
            return [{"displayName": name, "serviceId": name}
                    for name in ("Cloud Run", "Cloud Scheduler", "Secret Manager")]
        return google_skus(currency)

    monkeypatch.setattr(deploy_google_cloud, "billing_list", billing_list)
    for _ in range(2):
        estimate = deploy_google_cloud.cost_estimate(
            "my-project", "europe-west2", "0 6 * * *", "job", False, 0, True)
    assert paths == ["services", "services/Cloud Run/skus", "services/Cloud Scheduler/skus"]
    assert estimate.prices.startswith(f"europe-west2 list prices of {datetime.date.today()}")
