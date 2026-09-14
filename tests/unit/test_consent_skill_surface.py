"""Assertions on which consent-related skills are agent-callable.

The act/manipulate skills (`consent_grant`, `consent_revoke`,
`consent_request`, `consent_request_resolve`, `consent_request_list`)
have been un-exposed from the gateway after an activity-ledger audit
showed they had either 0% historical success rate (the act ones, which
were silently no-opping) or trivially low + workaround-shaped usage (the
manual `consent_request` / `consent_request_list` paths whose
documented use cases turn out to go through internal Python paths).

Read-only `consent_list` remains exposed for legitimate introspection
("what grants do I have right now?").

If any of the un-exposed skills are re-registered (e.g. a future
agent reaches for the historical Python entry points without realizing
the surface-level decision), this test fails loud.
"""

from work_buddy.mcp_server import registry


_UNEXPOSED_SKILLS = (
    "consent_grant",
    "consent_revoke",
    "consent_request",
    "consent_request_resolve",
    "consent_request_list",
)


def test_unexposed_consent_skills_are_not_in_registry() -> None:
    reg = registry.get_registry()
    found = [skill for skill in _UNEXPOSED_SKILLS if skill in reg]
    assert not found, (
        f"Skills re-exposed without re-audit: {found}. "
        "See knowledge/store/notifications/consent.md and the activity-ledger "
        "rationale in the original PR before re-registering."
    )


def test_consent_list_remains_exposed() -> None:
    reg = registry.get_registry()
    assert "consent_list" in reg, (
        "consent_list is the only legitimate agent-facing consent skill "
        "(read-only introspection). Removing it leaves agents with no way to "
        "check what grants their session holds."
    )
