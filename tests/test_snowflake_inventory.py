import inventory
from inventory import classify
from verify import listing

APPS = ["snowflake-a", "snowflake-b"]
MARK = "managed-by=pdt"
STORE = "managed-by=pdt pdt-lifecycle=retain"


def answers(sql):
    rows = {
        "SHOW DATABASES LIKE 'PDT%'": [
            {"name": "PDT", "comment": MARK},
            {"name": "PDT_DATA", "comment": STORE},
        ],
        "SHOW SCHEMAS IN DATABASE PDT": [
            {"name": "INFORMATION_SCHEMA", "comment": ""},
            {"name": "PUBLIC", "comment": ""},
            {"name": "PDT_SNOWFLAKE_A", "comment": f"{MARK} pdt-app=snowflake-a"},
            {"name": "PDT_SNOWFLAKE_B", "comment": f"{MARK} pdt-app=snowflake-b"},
        ],
        "SHOW TASKS IN SCHEMA PDT.PDT_SNOWFLAKE_A": [{"name": "RUN", "comment": MARK}],
        "SHOW USER FUNCTIONS IN SCHEMA PDT.PDT_SNOWFLAKE_A": [
            {"name": "READ_ENV", "description": MARK}],
        "SHOW JOB SERVICES IN SCHEMA PDT.PDT_SNOWFLAKE_B": [
            {"name": "RUN_20260929_000000", "comment": MARK}],
        "SHOW NETWORK RULES IN SCHEMA PDT.PUBLIC": [{"name": "PDT_EGRESS", "comment": MARK}],
        "SHOW TAGS IN SCHEMA PDT.PUBLIC": [{"name": "MANAGED_BY", "comment": MARK}],
        "SHOW COMPUTE POOLS LIKE 'PDT'": [{"name": "PDT", "comment": MARK}],
        "SHOW WAREHOUSES LIKE 'PDT'": [{"name": "PDT", "comment": MARK}],
    }
    return rows.get(sql, [])


def found(monkeypatch):
    monkeypatch.setattr(inventory, "snow", answers)
    return {resource.id: resource for resource in inventory.snowflake_inventory({"account": "x"})}


def test_every_object_pdt_made_is_listed_with_its_marker(monkeypatch):
    resources = found(monkeypatch)
    assert sorted(resources) == [
        "compute pool:PDT",
        "database:PDT",
        "database:PDT_DATA",
        "function:PDT.PDT_SNOWFLAKE_A.READ_ENV",
        "job service:PDT.PDT_SNOWFLAKE_B.RUN_20260929_000000",
        "network rule:PDT.PUBLIC.PDT_EGRESS",
        "schema:PDT.PDT_SNOWFLAKE_A",
        "schema:PDT.PDT_SNOWFLAKE_B",
        "tag:PDT.PUBLIC.MANAGED_BY",
        "task:PDT.PDT_SNOWFLAKE_A.RUN",
        "warehouse:PDT",
    ]
    assert all(resource.tags["managed-by"] == "pdt" for resource in resources.values())


def test_an_object_in_an_app_schema_carries_that_apps_tag(monkeypatch):
    resources = found(monkeypatch)
    assert resources["schema:PDT.PDT_SNOWFLAKE_A"].tags["pdt-app"] == "snowflake-a"
    assert resources["task:PDT.PDT_SNOWFLAKE_A.RUN"].tags["pdt-app"] == "snowflake-a"
    assert resources["job service:PDT.PDT_SNOWFLAKE_B.RUN_20260929_000000"].tags["pdt-app"] == "snowflake-b"
    assert "pdt-app" not in resources["compute pool:PDT"].tags


def test_the_data_store_is_retained_and_the_shared_objects_have_no_app(monkeypatch):
    resources = found(monkeypatch)
    assert resources["database:PDT_DATA"].tags["pdt-lifecycle"] == "retain"
    assert classify(resources["task:PDT.PDT_SNOWFLAKE_A.RUN"], APPS) == "snowflake-a"
    assert classify(resources["compute pool:PDT"], APPS) == "shared"
    assert classify(resources["database:PDT"], APPS) == "shared"
    assert classify(resources["tag:PDT.PUBLIC.MANAGED_BY"], APPS) == "shared"


def test_the_listing_drops_the_data_store(monkeypatch):
    monkeypatch.setattr(inventory, "snow", answers)
    names = [resource.id for resource in listing(lambda: inventory.snowflake_inventory({}))]
    assert "database:PDT_DATA" not in names
    assert "database:PDT" in names


def test_an_account_without_the_database_lists_only_the_account_objects(monkeypatch):
    def snow(sql):
        return [{"name": "PDT", "comment": MARK}] if sql == "SHOW WAREHOUSES LIKE 'PDT'" else []

    monkeypatch.setattr(inventory, "snow", snow)
    assert [r.id for r in inventory.snowflake_inventory({})] == ["warehouse:PDT"]
