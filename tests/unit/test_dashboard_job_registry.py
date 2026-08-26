from __future__ import annotations

from work_buddy.dashboard.job_registry import (
    job_registry_projection,
    search_job_registry,
)
from work_buddy.mcp_server.registry import Capability, WorkflowDefinition


def _registry():
    return {
        "morning-routine": WorkflowDefinition(
            name="morning-routine",
            description="Review the day and prepare a plan.\nLong detail.",
            workflow_file="knowledge/store/morning/morning-routine.md",
            execution="main",
            slash_command="wb-morning",
            params_schema={
                "same_day": {
                    "type": "bool",
                    "description": "Whether to stay on the current day.",
                    "required": False,
                }
            },
        ),
        "web_search": Capability(
            name="web_search",
            description="General web search.\nLong detail.",
            category="websearch",
            parameters={
                "query": {
                    "type": "str",
                    "description": "The search query.",
                    "required": True,
                }
            },
            callable=lambda **_kwargs: None,
        ),
    }


def test_projection_and_search_share_exact_authoring_metadata(monkeypatch):
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry", _registry
    )

    projection = job_registry_projection()
    assert projection["capabilities"] == [
        {
            "name": "web_search",
            "description": "General web search.",
            "parameters": [
                {
                    "name": "query",
                    "type": "str",
                    "description": "The search query.",
                    "required": True,
                }
            ],
            "slash_command": "",
        }
    ]
    assert projection["workflows"][0]["slash_command"] == "wb-morning"
    assert search_job_registry(
        reference_kind="job_capability", query="web search"
    ) == projection["capabilities"]
    assert search_job_registry(
        reference_kind="job_workflow", query="wb morning"
    ) == projection["workflows"]


def test_reference_search_is_bounded_and_never_dispatches(monkeypatch):
    registry = {
        f"match_{index}": Capability(
            name=f"match_{index}",
            description="Matching operation",
            category="test",
            parameters={},
            callable=lambda index=index: (_ for _ in ()).throw(
                AssertionError(f"dispatched {index}")
            ),
        )
        for index in range(12)
    }
    monkeypatch.setattr(
        "work_buddy.mcp_server.registry.get_registry", lambda: registry
    )

    result = search_job_registry(
        reference_kind="job_capability", query="matching", limit=100
    )
    assert len(result) == 8
    assert [item["name"] for item in result] == [
        "match_0",
        "match_1",
        "match_10",
        "match_11",
        "match_2",
        "match_3",
        "match_4",
        "match_5",
    ]
