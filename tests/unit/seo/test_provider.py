import json
from pathlib import Path

import httpx
import pytest

from octop.infra.seo.provider import (
    DataForSeoClient,
    ProviderFailure,
    interleave,
    map_labs,
    normalize_intent,
    normalize_keyword,
)

REFERENCE = json.loads(
    (Path(__file__).parents[2] / "fixtures/seo/openseo-reference.json").read_text()
)


def test_reference_normalization_and_interleave():
    for case in REFERENCE["normalization"]:
        assert normalize_keyword(case["input"]) == case["expected"]
    for case in REFERENCE["intents"]:
        assert normalize_intent(case["input"]) == case["expected"]
    for case in REFERENCE["interleave"]:
        actual = interleave(
            [{"keyword": k} for k in case["first"]],
            [{"keyword": k} for k in case["second"]],
            case["limit"],
        )
        assert [r["keyword"] for r in actual] == case["expected"]


def test_reference_labs_mapping_preserves_zero_and_unknown():
    rows = map_labs(REFERENCE["labs"]["items"])
    for row, expected in zip(rows, REFERENCE["labs"]["expected"], strict=True):
        assert row["keyword"] == expected["keyword"]
        assert row["search_volume"] == expected["searchVolume"]
        assert row["keyword_difficulty"] == expected["keywordDifficulty"]
        assert row["cpc"] == expected["cpc"]
        assert row["competition"] == expected["competition"]
        assert row["intent"] == expected["intent"]
    assert (
        map_labs([{"keyword": "zero", "keyword_info": {"search_volume": 0}}])[0]["search_volume"]
        == 0
    )
    assert map_labs([{"keyword": "unknown"}])[0]["search_volume"] is None


@pytest.mark.parametrize("status,task_status,cost", [(200, 40501, 0.012), (503, 50000, 0.04)])
async def test_task_failure_retains_cost_without_retry(status, task_status, cost):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"status_code": 20000, "tasks": [{"status_code": task_status, "cost": cost}]},
        )

    client = DataForSeoClient("login", "password", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderFailure) as error:
        await client.fetch(
            source="ideas",
            seed="test",
            location_code=2840,
            language_code="en",
            limit=150,
            clickstream=False,
            group_keywords=True,
        )
    assert error.value.cost == cost
    assert len(requests) == 1
    assert requests[0].url.host == "api.dataforseo.com"
    assert json.loads(requests[0].content)[0]["keywords"] == ["test"]


async def test_timeout_unknown_without_retry():
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("unknown delivery")

    with pytest.raises(ProviderFailure) as error:
        await DataForSeoClient("login", "password", transport=httpx.MockTransport(handler)).fetch(
            source="related",
            seed="test",
            location_code=2840,
            language_code="en",
            limit=150,
            clickstream=False,
            group_keywords=True,
        )
    assert error.value.unknown and error.value.cost is None
    assert len(requests) == 1


async def test_empty_success_and_receipt():
    client = DataForSeoClient(
        "login",
        "password",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "status_code": 20000,
                    "tasks": [
                        {"id": "upstream-id", "status_code": 20000, "cost": 0.01, "result": None}
                    ],
                },
            )
        ),
    )
    rows, cost, receipt = await client.fetch(
        source="ideas",
        seed="empty",
        location_code=2840,
        language_code="en",
        limit=150,
        clickstream=False,
        group_keywords=True,
    )
    assert rows == [] and cost == 0.01
    assert receipt == {
        "http_status": 200,
        "status_code": 20000,
        "task_id": "upstream-id",
        "task_status": 20000,
    }
