"""Project background: OpenSEO's ops, normalization, caps and atomic batches."""

import pytest

from octop.infra.errors import OctopError

STANDARD = ["business_overview", "current_goal", "positioning", "writing_preferences"]


async def project(context):
    service, repos, user, _ = context
    created = await service.create_project(
        user, name="Example", domain="example.com", agent_id="seo"
    )
    return created["project_id"]


async def test_context_ops_normalize_upsert_and_clear(context):
    service, repos, user, _ = context
    pid = await project(context)
    empty = await service.project_context(user, pid)
    assert empty["missing_sections"] == STANDARD and empty["research_log"] == []
    data = await service.update_context(
        user,
        pid,
        [
            {"section": "current_goal", "content": "  Grow signups  "},
            {"custom_section": "launch-plan", "title": " Launch ", "content": "Q4"},
            # One competitor entered twice collapses to its last occurrence.
            {
                "add_competitors": [
                    {"domain": "https://www.Rival.com/pricing", "name": "Rival"},
                    {"domain": "rival.com", "notes": "main"},
                ]
            },
            {
                "add_key_pages": [
                    {"url": "www.Example.com/Pricing?plan=a#top", "role": "money"},
                    {"url": "http://example.com/", "topic": "home"},
                ]
            },
        ],
        author="user",
    )
    assert [(s["key"], s["content"], s["updated_by"]) for s in data["sections"]] == [
        ("current_goal", "Grow signups", "user")
    ]
    assert data["missing_sections"] == [k for k in STANDARD if k != "current_goal"]
    assert [(c["slug"], c["title"], c["content"]) for c in data["custom_sections"]] == [
        ("launch-plan", "Launch", "Q4")
    ]
    assert [(c["domain"], c["name"], c["notes"]) for c in data["competitors"]] == [
        ("rival.com", None, "main")
    ]
    assert [(p["url"], p["role"], p["topic"]) for p in data["key_pages"]] == [
        ("https://example.com", "other", "home"),
        ("https://example.com/Pricing?plan=a", "money", None),
    ]
    data = await service.update_context(
        user,
        pid,
        [
            # Re-adding a page without a role keeps its role; blanks keep stored text.
            {"add_key_pages": [{"url": "https://example.com/Pricing?plan=a", "topic": "price"}]},
            {"add_competitors": [{"domain": "RIVAL.com", "name": "Rival Inc"}]},
            {"section": "current_goal", "content": ""},
            {"delete_custom_section": "launch-plan"},
            {"remove_key_pages": ["example.com"]},
        ],
        author="agent",
    )
    assert data["missing_sections"] == STANDARD and data["custom_sections"] == []
    assert [(p["url"], p["role"], p["topic"], p["updated_by"]) for p in data["key_pages"]] == [
        ("https://example.com/Pricing?plan=a", "money", "price", "agent")
    ]
    assert [(c["name"], c["notes"]) for c in data["competitors"]] == [("Rival Inc", "main")]
    data = await service.update_context(
        user, pid, [{"remove_competitors": ["https://WWW.rival.com"]}], author="user"
    )
    assert data["competitors"] == []


async def test_context_batches_are_atomic_and_capped(context):
    service, repos, user, _ = context
    pid = await project(context)
    with pytest.raises(OctopError) as rejected:
        await service.update_context(
            user,
            pid,
            [
                {"section": "positioning", "content": "not written"},
                {"section": "current_goal", "content": "x" * 4001},
            ],
            author="user",
        )
    assert "updates[1]" in str(rejected.value)
    assert (await service.project_context(user, pid))["sections"] == []
    await service.update_context(
        user,
        pid,
        [{"add_competitors": [{"domain": f"c{i}.com"} for i in range(100)]}],
        author="user",
    )
    with pytest.raises(OctopError):
        await service.update_context(
            user, pid, [{"add_competitors": [{"domain": "c100.com"}]}], author="user"
        )
    # Updating a known competitor at the cap is fine.
    await service.update_context(
        user, pid, [{"add_competitors": [{"domain": "c5.com", "notes": "n"}]}], author="user"
    )
    # One batch cannot slip past a cap by splitting it across ops.
    with pytest.raises(OctopError) as too_many:
        await service.update_context(
            user,
            pid,
            [{"custom_section": f"s{i}", "content": "x"} for i in range(21)],
            author="user",
        )
    assert "updates[20]" in str(too_many.value)
    assert (await service.project_context(user, pid))["custom_sections"] == []
    for bad in (
        [],
        [{"section": "unknown", "content": "x"}],
        [{"section": "current_goal", "contents": "x"}],
        [{"custom_section": "Bad_Slug", "content": "x"}],
        [{"add_competitors": [{"domain": "not a domain"}]}],
        [{"add_key_pages": [{"url": "ftp://example.com/a"}]}],
        [{"add_key_pages": [{"url": "https://user:pw@example.com/a"}]}],
        [{"append_research_log": {"summary": "   "}}],
    ):
        with pytest.raises(OctopError):
            await service.update_context(user, pid, bad, author="user")


async def test_research_log_is_newest_first_capped_and_pruned(context):
    service, repos, user, _ = context
    pid = await project(context)
    for index in range(22):
        await service.update_context(
            user, pid, [{"append_research_log": {"summary": f"entry {index}"}}], author="agent"
        )
    log = (await service.project_context(user, pid))["research_log"]
    assert len(log) == 20 and log[0]["summary"] == "entry 21"
    assert log[0]["created_by"] == "agent"
    with repos.seo_repo._db.transaction() as conn:
        conn.execute("UPDATE seo_research_log SET entry_date='2000-01-01' WHERE summary='entry 0'")
    data = await service.update_context(
        user,
        pid,
        [
            {"append_research_log": {"summary": "entry 22"}},
            {"remove_research_log": [log[-1]["log_id"]]},
        ],
        author="user",
    )
    with repos.seo_repo._db.connect() as conn:
        left = [row["summary"] for row in conn.execute("SELECT summary FROM seo_research_log")]
    # entry 0 was pruned on append and entry 2 removed by id.
    assert "entry 0" not in left and "entry 2" not in left and len(left) == 21
    assert data["research_log"][0]["summary"] == "entry 22"
