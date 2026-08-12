"""Trusted host-launch delivery for one-time dashboard bootstrap grants."""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from work_buddy.security.local_identity import (
    DEFAULT_AUDIENCE,
    LocalIdentityAuthority,
    get_default_authority,
    normalize_loopback_origin,
)


def bootstrap_fragment_for_dashboard(
    app_url: str,
    *,
    next_hash: str = "",
    authority: LocalIdentityAuthority | None = None,
) -> str:
    """Mint a grant and encode it in a fragment that never reaches HTTP logs.

    This function is for trusted local host launchers (tray/desktop/CLI), not a
    request handler.  Browser code must remove the fragment from history before
    attempting redemption.
    """

    parsed = urlsplit(app_url)
    origin = normalize_loopback_origin(f"{parsed.scheme}://{parsed.netloc}")
    if next_hash and (not next_hash.startswith("#") or len(next_hash) > 4096):
        raise ValueError("next_hash must be an optional bounded URL fragment")
    service = authority or get_default_authority()
    grant = service.mint_bootstrap(origin=origin, audience=DEFAULT_AUDIENCE)
    values = {"wb-bootstrap": grant.token}
    if next_hash:
        values["wb-next"] = next_hash
    return f"#{urlencode(values)}"


__all__ = ["bootstrap_fragment_for_dashboard"]
