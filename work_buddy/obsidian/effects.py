"""Effect manifests for multi-effect capabilities.

Capabilities that produce multiple externally-visible side effects
(e.g. ``task_create`` writes a note file AND appends a line to the
master task list) declare their effects via a manifest. The
post-write verification path (see ``post_write_verify``) walks the
manifest at PWU recovery time so the verifier can detect
"some-effects-landed-some-didn't" partial states — the failure mode
that ``t-e2f1a8c4`` documented.

Without manifests, verify-first only checks the file path on the
``ObsidianPostWriteUncertain`` exception (the FIRST effect's path).
A successful first effect made the verifier declare "verified" while
the second effect was silently missing.

Schema is intentionally narrow: each effect declares (1) what KIND of
write it is, (2) where it lands (literal path or template), (3) how
to verify it landed (witness substring + mode). Generated values
(e.g. uuid-derived paths) are resolved via an optional resolver
callable that pulls from the capability's idempotency cache or
similar side channel.

Capabilities WITHOUT a declared manifest fall back to the existing
single-effect behavior — backward compat preserved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal

# Effect verification semantics — must align with the corresponding
# write_mode strings used by ``ObsidianPostWriteUncertain``:
#   - "substring": witness IS in the file content (insert/append/replace
#     with substring witness)
#   - "sha256":    full-file sha256 matches the witness ("sha256:<hex>")
#   - "absent":    witness is NOT in the file content (delete-style)
WitnessMode = Literal["substring", "sha256", "absent"]

# Coarse classification of an effect — informational, not used by the
# verifier directly today, but useful for logging and future
# sub-step dispatch work.
EffectKind = Literal["file_write", "line_append", "file_delete"]


@dataclass(frozen=True)
class EffectSpec:
    """One declared external effect of a capability.

    Either ``path`` (literal) OR ``path_template`` (string with ``{name}``
    placeholders resolved from params + resolver output) must be set,
    not both.

    Either ``witness_template`` (placeholders resolved like
    ``path_template``) or ``witness`` (literal) may be set. If neither
    is set, the verifier falls back to checking only the file's
    existence at the resolved path.

    ``resolver`` is an optional callable that receives the
    capability's ``params`` dict and returns a dict of additional
    values to inject into template substitution. The intended use is
    pulling generated values (uuids, task_ids) from a side channel —
    typically the capability's idempotency cache (e.g.
    ``mutations._resolve_idempotent_create_ids`` for ``task_create``).

    A resolver returning a dict with any-None values is treated as
    "couldn't resolve this effect" — the verifier marks that effect
    indeterminate rather than crashing.
    """

    kind: EffectKind
    path: str | None = None
    path_template: str | None = None
    witness: str | None = None
    witness_template: str | None = None
    witness_mode: WitnessMode = "substring"
    resolver: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None

    def resolve_path(
        self, params: dict[str, Any], generated: dict[str, Any] | None,
    ) -> str | None:
        """Materialize the path. Returns None if templating fails
        (e.g. a required placeholder is missing)."""
        if self.path:
            return self.path
        if self.path_template is None:
            return None
        ctx = dict(params or {})
        if generated:
            ctx.update(generated)
        try:
            return self.path_template.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return None

    def resolve_witness(
        self, params: dict[str, Any], generated: dict[str, Any] | None,
    ) -> str | None:
        """Materialize the witness, or None if not specified / unresolvable."""
        if self.witness:
            return self.witness
        if self.witness_template is None:
            return None
        ctx = dict(params or {})
        if generated:
            ctx.update(generated)
        try:
            return self.witness_template.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return None

    def call_resolver(
        self, params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Invoke the resolver (if any) to get generated values.

        Returns:
            dict of generated values on success
            None if no resolver OR resolver couldn't resolve
            (capability cache miss / templating-relevant value missing)
        """
        if self.resolver is None:
            return {}
        try:
            result = self.resolver(params or {})
        except Exception:
            return None
        if result is None:
            return None
        # A resolver-returned dict with any-None values means partial
        # resolution — caller treats that effect as indeterminate.
        if any(v is None for v in result.values()):
            return None
        return result
