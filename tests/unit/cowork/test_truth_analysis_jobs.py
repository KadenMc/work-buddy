from __future__ import annotations

from work_buddy.agent_execution.models import (
    AgentExecutionSelection,
    AgentSpawnOutcome,
    AgentSpawnRequest,
)
from work_buddy.cowork.execution_identity import (
    cowork_generation_from_session,
    cowork_truth_analysis_run_from_session,
    cowork_truth_analysis_session_id,
)
from work_buddy.cowork.truth_analysis_jobs import (
    TRUTH_ANALYSIS_FETCH_CAPABILITY,
    TRUTH_ANALYSIS_JOB_GET_CAPABILITY,
    TRUTH_ANALYSIS_JOB_SUBMIT_CAPABILITY,
    TRUTH_ANALYSIS_SEARCH_CAPABILITY,
    build_truth_analysis_prompt,
    spawn_truth_analysis_job,
)
from work_buddy.mcp_server import session_acl


SELECTION = AgentExecutionSelection(
    provider_id="codex",
    model_id="gpt-5.6-sol",
    provider_label="Codex",
    model_label="GPT-5.6 Sol",
)


def test_truth_analysis_identity_and_builtin_acl_are_least_authority():
    run_id = "a" * 32
    session_id = cowork_truth_analysis_session_id(run_id)

    assert cowork_truth_analysis_run_from_session(session_id) == run_id
    assert cowork_generation_from_session(session_id) is None
    assert session_acl.get_session_acl(session_id) == frozenset(
        {
            TRUTH_ANALYSIS_JOB_GET_CAPABILITY,
            TRUTH_ANALYSIS_SEARCH_CAPABILITY,
            TRUTH_ANALYSIS_FETCH_CAPABILITY,
            TRUTH_ANALYSIS_JOB_SUBMIT_CAPABILITY,
        }
    )
    for forbidden in (
        "cowork_doc_get",
        "cowork_doc_propose_edit",
        "truth_claim_propose",
        "truth_claim_confirm",
        "conversation_send",
        "web_fetch",
        "task_toggle",
    ):
        assert not session_acl.is_capability_allowed(session_id, forbidden)


def test_prompt_is_deterministic_tool_delivered_and_denies_truth_writes():
    kwargs = {
        "store_id": "store-1",
        "document_id": "doc-1",
        "run_id": "b" * 32,
        "selection": SELECTION,
    }
    first = build_truth_analysis_prompt(**kwargs)

    assert first == build_truth_analysis_prompt(**kwargs)
    assert "WORK_BUDDY_SESSION_ID" in first
    assert TRUTH_ANALYSIS_JOB_GET_CAPABILITY in first
    assert TRUTH_ANALYSIS_SEARCH_CAPABILITY in first
    assert TRUTH_ANALYSIS_FETCH_CAPABILITY in first
    assert TRUTH_ANALYSIS_JOB_SUBMIT_CAPABILITY in first
    assert "no arbitrary URL fetch" in first
    assert "cannot write to the Truth" in first
    assert "untrusted data" in first


def test_spawn_uses_exact_account_backed_selection_and_session():
    requests: list[AgentSpawnRequest] = []

    def _spawn(request: AgentSpawnRequest) -> AgentSpawnOutcome:
        requests.append(request)
        return AgentSpawnOutcome(
            status="ok",
            selection=SELECTION,
            pid=321,
            session_id=request.session_id,
        )

    metadata = spawn_truth_analysis_job(
        store_id="store-1",
        document_id="doc-1",
        run_id="c" * 32,
        selection=SELECTION,
        spawn_detached=_spawn,
    )

    assert metadata.ok is True
    assert metadata.pid == 321
    assert len(requests) == 1
    assert requests[0].selection is SELECTION
    assert requests[0].session_id == cowork_truth_analysis_session_id("c" * 32)
    assert requests[0].max_budget_usd == 2.0
