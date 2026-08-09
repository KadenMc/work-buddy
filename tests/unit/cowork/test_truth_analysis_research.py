from __future__ import annotations

import re
from collections.abc import Sequence

import pytest

from work_buddy.cowork import truth_analysis_research as research
from work_buddy.cowork import truth_analysis_runtime as runtime
from work_buddy.truth.identity import sha256_text
from work_buddy.websearch.errors import WebSearchUnavailable
from work_buddy.websearch.models import SearchHit


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "_DB_PATH", tmp_path / "truth-analysis.db")


def _run(
    *,
    run_id: str = "a" * 32,
    session_id: str | None = None,
):
    created = runtime.create_run(
        run_id=run_id,
        store_id="store-alpha",
        document_id=run_id,
        action_snapshot_id="b" * 32,
        selection={
            "provider_id": "claude-code",
            "model_id": "sonnet",
            "provider_label": "Claude Code",
            "model_label": "Sonnet",
        },
        authorization_receipt_id="c" * 32,
        context_sha256="e" * 64,
        request={
            "schema": "wb.cowork.truth-analysis-request/v1",
            "source_coverage": [
                {
                    "source": "selected_passage",
                    "status": "searched",
                    "detail": "Exact passage.",
                    "external_egress": False,
                },
                {
                    "source": "web",
                    "status": "not_searched",
                    "detail": "No search yet.",
                    "external_egress": False,
                },
            ],
        },
        session_id=session_id or f"{run_id}-truth-analysis",
        at="2026-08-09T12:00:00+00:00",
    )
    return runtime.update_run(created.run_id, status="running", pid=42)


def _hits(count: int = 7, *, host: str = "example.com") -> list[SearchHit]:
    return [
        SearchHit(
            title=f"Source {index}",
            url=f"https://{host}/source-{index}",
            snippet=f"Lead snippet {index}",
            provider="fixture-search",
            published="2026-08-09T00:00:00Z",
            score=1.0 - index / 100,
            raw_text=f"Provider-inline body {index} must not be admitted.",
        )
        for index in range(count)
    ]


def _search(run, query: str = "bounded claim evidence", *, hits=None):
    supplied = _hits() if hits is None else hits
    return research.search(
        run_id=run.run_id,
        query=query,
        agent_session_id=run.session_id,
        searcher=lambda _query, **_kwargs: supplied,
    )


def test_search_is_run_bound_capped_lead_only_and_replay_safe():
    run = _run()
    calls: list[tuple[str, int, bool]] = []

    def searcher(query: str, *, max_results: int, cache: bool):
        calls.append((query, max_results, cache))
        return _hits()

    first = research.search(
        run_id=run.run_id,
        query="  bounded   claim evidence ",
        agent_session_id=run.session_id,
        searcher=searcher,
    )
    replay = research.search(
        run_id=run.run_id,
        query="bounded claim evidence",
        agent_session_id=run.session_id,
        searcher=lambda *_args, **_kwargs: pytest.fail("search replay escaped"),
    )

    assert calls == [("bounded claim evidence", 5, False)]
    assert first.status == "completed"
    assert len(first.hits) == research.MAX_HITS_PER_QUERY
    assert replay.replayed is True
    assert [item.hit_id for item in replay.hits] == [
        item.hit_id for item in first.hits
    ]
    assert all(re.fullmatch(r"[0-9a-f]{32}", item.hit_id) for item in first.hits)
    assert all(item.lead_only for item in first.hits)
    assert "Provider-inline body" not in str(first.to_dict())
    persisted = runtime.search_receipts_for_run(run.run_id)[0]
    assert all("raw_text" not in item for item in persisted.hits)
    assert all(item["lead_only"] is True for item in persisted.hits)


def test_three_distinct_queries_are_allowed_and_a_fourth_is_rejected():
    run = _run()
    for index in range(research.MAX_QUERIES_PER_RUN):
        _search(run, f"query {index}")

    with pytest.raises(
        research.TruthAnalysisResearchError, match="query limit"
    ) as captured:
        _search(run, "one query too many")

    assert captured.value.code == "search_limit_reached"
    assert len(runtime.search_receipts_for_run(run.run_id)) == 3


def test_failed_search_is_persisted_and_replayed_without_second_egress():
    run = _run()
    calls = 0

    def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise WebSearchUnavailable("provider detail must not escape")

    first = research.search(
        run_id=run.run_id,
        query="unavailable source",
        agent_session_id=run.session_id,
        searcher=unavailable,
    )
    replay = research.search(
        run_id=run.run_id,
        query="unavailable source",
        agent_session_id=run.session_id,
        searcher=unavailable,
    )

    assert calls == 1
    assert first.status == replay.status == "failed"
    assert first.error_code == "websearch_unavailable"
    assert "provider detail" not in first.error
    assert replay.replayed is True


def test_search_requires_the_exact_worker_session_and_an_active_run():
    run = _run()
    with pytest.raises(research.TruthAnalysisResearchError) as forbidden:
        research.search(
            run_id=run.run_id,
            query="query",
            agent_session_id="another-worker",
            searcher=lambda *_args, **_kwargs: (),
        )
    assert forbidden.value.code == "analysis_session_forbidden"

    runtime.update_run(run.run_id, status="failed", error_code="stopped")
    with pytest.raises(research.TruthAnalysisResearchError) as terminal:
        research.search(
            run_id=run.run_id,
            query="query",
            agent_session_id=run.session_id,
            searcher=lambda *_args, **_kwargs: (),
        )
    assert terminal.value.code == "analysis_run_terminal"


def test_search_filters_non_http_private_literal_and_duplicate_leads():
    run = _run()
    hits = [
        SearchHit("Script", "javascript:alert(1)", "bad", "fixture"),
        SearchHit("Loopback", "http://127.0.0.1/private", "bad", "fixture"),
        SearchHit("Good", "https://example.com/source", "lead", "fixture"),
        SearchHit("Duplicate", "https://example.com/source", "lead", "fixture"),
    ]

    result = _search(run, hits=hits)

    assert [(item.title, item.url) for item in result.hits] == [
        ("Good", "https://example.com/source")
    ]


def test_fetch_validates_each_redirect_and_persists_exact_acquisition():
    run = _run()
    lead = _search(run, hits=_hits(1)).hits[0]
    resolved: list[tuple[str, int]] = []
    requested: list[str] = []

    def resolver(host: str, port: int) -> Sequence[str]:
        resolved.append((host, port))
        return ("93.184.216.34",)

    def requester(
        url: str,
        addresses: Sequence[str],
        timeout_s: float,
        max_bytes: int,
    ):
        requested.append(url)
        assert addresses == ("93.184.216.34",)
        assert 0 < timeout_s <= research.MAX_REQUEST_SECONDS
        assert max_bytes == research.MAX_RESPONSE_BYTES
        if url == "https://example.com/source-0":
            return research.ResearchHttpResponse(
                302,
                {"Location": "https://sources.example/final"},
                b"",
            )
        return research.ResearchHttpResponse(
            200,
            {"Content-Type": "text/plain; charset=utf-8"},
            b"Exact fetched source text.\n",
        )

    first = research.fetch(
        run_id=run.run_id,
        hit_id=lead.hit_id,
        agent_session_id=run.session_id,
        resolver=resolver,
        requester=requester,
    )
    receipt = first.receipt

    assert resolved == [("example.com", 443), ("sources.example", 443)]
    assert requested == [
        "https://example.com/source-0",
        "https://sources.example/final",
    ]
    assert receipt.status == "completed"
    assert receipt.exact_text == "Exact fetched source text.\n"
    assert receipt.text_sha256 == sha256_text(receipt.exact_text)
    assert receipt.requested_url == "https://example.com/source-0"
    assert receipt.source_url == "https://sources.example/final"
    assert receipt.title == "Source 0"
    assert receipt.redirect_chain == tuple(requested)
    assert receipt.acquisition_metadata["method"] == "guarded_direct_http_get"
    assert receipt.acquisition_metadata["bytes_received"] == 27
    assert receipt.acquisition_metadata["text_truncated"] is False
    assert receipt.acquisition_metadata["captured_text_sha256"] == receipt.text_sha256
    runtime_receipt = runtime.get_fetch_receipt(run.run_id, receipt.fetch_id)
    assert runtime_receipt is not None
    assert runtime_receipt.text == receipt.exact_text
    assert runtime_receipt.content_sha256 == receipt.text_sha256

    replay = research.fetch(
        run_id=run.run_id,
        hit_id=lead.hit_id,
        agent_session_id=run.session_id,
        resolver=lambda *_args: pytest.fail("replay resolved a host"),
        requester=lambda *_args: pytest.fail("replay fetched a URL"),
    )
    looked_up = research.get_receipt(
        run_id=run.run_id,
        fetch_id=receipt.fetch_id,
        agent_session_id=run.session_id,
    )
    assert replay.replayed is True
    assert replay.receipt == receipt
    assert looked_up == receipt
    assert research.receipts_for_run(
        run_id=run.run_id,
        agent_session_id=run.session_id,
    ) == (receipt,)


def test_private_redirect_is_rejected_before_the_redirect_target_is_requested():
    run = _run()
    lead = _search(run, hits=_hits(1)).hits[0]
    requested: list[str] = []

    def requester(
        url: str,
        _addresses: Sequence[str],
        _timeout_s: float,
        _max_bytes: int,
    ):
        requested.append(url)
        return research.ResearchHttpResponse(
            302,
            {"location": "http://127.0.0.1/admin"},
            b"",
        )

    result = research.fetch(
        run_id=run.run_id,
        hit_id=lead.hit_id,
        agent_session_id=run.session_id,
        resolver=lambda *_args: ("93.184.216.34",),
        requester=requester,
    )

    assert requested == ["https://example.com/source-0"]
    assert result.receipt.status == "failed"
    assert result.receipt.error_code == "unsafe_destination"
    assert result.receipt.external_egress is True


def test_nonstandard_port_on_admitted_legacy_hit_is_rejected_before_network():
    run = _run()
    hit_id = "9" * 32
    runtime.record_search_receipt(
        run_id=run.run_id,
        query="legacy poisoned port",
        status="completed",
        hits=[
            {
                "hit_id": hit_id,
                "title": "Unsafe public service",
                "url": "https://example.com:8443/private-service",
                "snippet": "Lead only.",
                "provider": "legacy-fixture",
                "lead_only": True,
            }
        ],
        external_egress=False,
        max_searches=research.MAX_QUERIES_PER_RUN,
    )
    calls = []

    result = research.fetch(
        run_id=run.run_id,
        hit_id=hit_id,
        agent_session_id=run.session_id,
        resolver=lambda *_args: calls.append("resolve") or ("93.184.216.34",),
        requester=lambda *_args: calls.append("request")
        or pytest.fail("nonstandard port reached HTTP"),
    )

    assert calls == []
    assert result.receipt.status == "failed"
    assert result.receipt.error_code == "unsafe_destination"
    assert result.receipt.external_egress is False


def test_nonstandard_port_redirect_is_rejected_before_target_resolution():
    run = _run()
    lead = _search(run, hits=_hits(1)).hits[0]
    resolved = []
    requested = []

    def requester(url, *_args):
        requested.append(url)
        return research.ResearchHttpResponse(
            302,
            {"location": "https://example.com:8443/private-service"},
            b"",
        )

    result = research.fetch(
        run_id=run.run_id,
        hit_id=lead.hit_id,
        agent_session_id=run.session_id,
        resolver=lambda host, port: resolved.append((host, port))
        or ("93.184.216.34",),
        requester=requester,
    )

    assert resolved == [("example.com", 443)]
    assert requested == ["https://example.com/source-0"]
    assert result.receipt.status == "failed"
    assert result.receipt.error_code == "unsafe_destination"


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.0.0.8",),
        ("169.254.169.254",),
        ("::1",),
        ("fe80::1",),
        ("93.184.216.34", "10.0.0.8"),
    ],
)
def test_private_or_mixed_dns_answers_never_reach_http(addresses):
    run = _run()
    lead = _search(run, hits=_hits(1, host="source.example")).hits[0]
    calls = 0

    def requester(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("unsafe destination reached HTTP")

    result = research.fetch(
        run_id=run.run_id,
        hit_id=lead.hit_id,
        agent_session_id=run.session_id,
        resolver=lambda *_args: addresses,
        requester=requester,
    )

    assert calls == 0
    assert result.receipt.status == "failed"
    assert result.receipt.error_code == "unsafe_destination"
    assert result.receipt.external_egress is True


def test_default_http_request_pins_validated_ip_but_verifies_original_hostname(
    monkeypatch,
):
    events: dict[str, object] = {}

    class FakeSocket:
        def settimeout(self, value):
            events["socket_timeout"] = value

        def close(self):
            events["socket_closed"] = True

    sock = FakeSocket()

    def connected_socket(*, address: str, port: int, timeout_s: float):
        events["connect"] = (address, port, timeout_s)
        return sock

    class FakeContext:
        def wrap_socket(self, value, *, server_hostname: str):
            assert value is sock
            events["tls_hostname"] = server_hostname
            return value

    class FakeResponse:
        status = 200

        def getheaders(self):
            return [("Content-Type", "text/plain")]

        def read(self, _size):
            if events.get("read"):
                return b""
            events["read"] = True
            return b"Pinned response"

    class FakeConnection:
        def __init__(self, host, port, timeout):
            events["connection"] = (host, port, timeout)
            self.sock = None

        def request(self, method, target, *, headers):
            assert self.sock is sock
            events["request"] = (method, target, dict(headers))

        def getresponse(self):
            return FakeResponse()

        def close(self):
            events["connection_closed"] = True

    monkeypatch.setattr(research, "_connected_socket", connected_socket)
    monkeypatch.setattr(research.ssl, "create_default_context", FakeContext)
    monkeypatch.setattr(research.http.client, "HTTPConnection", FakeConnection)

    response = research._default_requester(
        "https://public.example/source?q=one",
        ("93.184.216.34",),
        5.0,
        1_024,
    )

    assert events["connect"][:2] == ("93.184.216.34", 443)
    assert events["tls_hostname"] == "public.example"
    method, target, headers = events["request"]
    assert (method, target) == ("GET", "/source?q=one")
    assert headers["Host"] == "public.example"
    assert response.body == b"Pinned response"


def test_fetch_rejects_unadmitted_and_cross_run_hit_ids():
    first = _run(run_id="a" * 32)
    second = _run(run_id="f" * 32)
    lead = _search(first, hits=_hits(1)).hits[0]

    for run, hit_id in ((first, "not-admitted"), (second, lead.hit_id)):
        with pytest.raises(research.TruthAnalysisResearchError) as captured:
            research.fetch(
                run_id=run.run_id,
                hit_id=hit_id,
                agent_session_id=run.session_id,
                resolver=lambda *_args: ("93.184.216.34",),
                requester=lambda *_args: pytest.fail("unadmitted URL fetched"),
            )
        assert captured.value.code == "search_hit_not_admitted"


def test_response_size_and_redirect_loop_are_bounded_and_receipted():
    oversized_run = _run(run_id="1" * 32)
    oversized_lead = _search(oversized_run, hits=_hits(1)).hits[0]
    oversized = research.fetch(
        run_id=oversized_run.run_id,
        hit_id=oversized_lead.hit_id,
        agent_session_id=oversized_run.session_id,
        resolver=lambda *_args: ("93.184.216.34",),
        requester=lambda *_args: research.ResearchHttpResponse(
            200,
            {"content-type": "text/plain"},
            b"x" * (research.MAX_RESPONSE_BYTES + 1),
        ),
    )
    assert oversized.receipt.status == "failed"
    assert oversized.receipt.error_code == "response_too_large"

    loop_run = _run(run_id="2" * 32)
    loop_lead = _search(loop_run, hits=_hits(1)).hits[0]
    looped = research.fetch(
        run_id=loop_run.run_id,
        hit_id=loop_lead.hit_id,
        agent_session_id=loop_run.session_id,
        resolver=lambda *_args: ("93.184.216.34",),
        requester=lambda url, *_args: research.ResearchHttpResponse(
            302,
            {"location": url},
            b"",
        ),
    )
    assert looped.receipt.status == "failed"
    assert looped.receipt.error_code == "redirect_loop"


def test_model_facing_text_is_utf8_bounded_with_explicit_full_text_integrity():
    run = _run()
    lead = _search(run, hits=_hits(1)).hits[0]
    full_text = "é" * research.MAX_CAPTURED_TEXT_BYTES
    full_bytes = full_text.encode("utf-8")

    fetched = research.fetch(
        run_id=run.run_id,
        hit_id=lead.hit_id,
        agent_session_id=run.session_id,
        resolver=lambda *_args: ("93.184.216.34",),
        requester=lambda *_args: research.ResearchHttpResponse(
            200,
            {"content-type": "text/plain; charset=utf-8"},
            full_bytes,
        ),
    )
    receipt = fetched.receipt

    assert receipt.status == "completed"
    assert len(receipt.exact_text.encode("utf-8")) <= research.MAX_CAPTURED_TEXT_BYTES
    assert receipt.acquisition_metadata["text_truncated"] is True
    assert receipt.acquisition_metadata["extracted_text_bytes"] == len(full_bytes)
    assert receipt.acquisition_metadata["captured_text_bytes"] == len(
        receipt.exact_text.encode("utf-8")
    )
    assert receipt.acquisition_metadata["captured_text_sha256"] == receipt.text_sha256
    assert receipt.acquisition_metadata["full_extracted_text_sha256"] == sha256_text(
        full_text
    )


def test_non_identity_content_encoding_is_rejected_instead_of_misdecoded():
    run = _run()
    lead = _search(run, hits=_hits(1)).hits[0]

    fetched = research.fetch(
        run_id=run.run_id,
        hit_id=lead.hit_id,
        agent_session_id=run.session_id,
        resolver=lambda *_args: ("93.184.216.34",),
        requester=lambda *_args: research.ResearchHttpResponse(
            200,
            {
                "content-type": "text/plain; charset=utf-8",
                "content-encoding": "gzip",
            },
            b"not actually safe decoded text",
        ),
    )

    assert fetched.receipt.status == "failed"
    assert fetched.receipt.error_code == "unsupported_content_encoding"
    assert fetched.receipt.exact_text == ""


def test_stale_pending_operation_fails_closed_as_durable_outcome_unknown():
    run = _run()
    query = "crashed outbound query"
    operation, reserved = research._reserve_operation(
        run.run_id,
        "search",
        sha256_text(query),
        maximum=research.MAX_QUERIES_PER_RUN,
    )
    assert reserved is True
    with research._connect() as conn:
        conn.execute(
            "UPDATE cowork_truth_analysis_research_operations "
            "SET updated_at = ? WHERE operation_id = ?",
            ("2020-01-01T00:00:00+00:00", str(operation["operation_id"])),
        )
    calls = 0

    def must_not_replay(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return _hits(1)

    with pytest.raises(research.TruthAnalysisResearchError) as captured:
        research.search(
            run_id=run.run_id,
            query=query,
            agent_session_id=run.session_id,
            searcher=must_not_replay,
        )

    assert calls == 0
    assert captured.value.code == "research_outcome_unknown"
    assert captured.value.retryable is False
    with research._connect() as conn:
        state = conn.execute(
            "SELECT state FROM cowork_truth_analysis_research_operations "
            "WHERE operation_id = ?",
            (str(operation["operation_id"]),),
        ).fetchone()[0]
    assert state == "outcome_unknown"


def test_receipt_lookup_remains_available_after_run_is_terminal():
    run = _run()
    lead = _search(run, hits=_hits(1)).hits[0]
    fetched = research.fetch(
        run_id=run.run_id,
        hit_id=lead.hit_id,
        agent_session_id=run.session_id,
        resolver=lambda *_args: ("93.184.216.34",),
        requester=lambda *_args: research.ResearchHttpResponse(
            200,
            {"content-type": "text/plain"},
            b"Durable text",
        ),
    )
    runtime.update_run(run.run_id, status="failed", error_code="worker_done")

    receipt = research.get_receipt(
        run_id=run.run_id,
        fetch_id=fetched.receipt.fetch_id,
        agent_session_id=run.session_id,
    )

    assert receipt == fetched.receipt
