"""Post-write verification recovery for ObsidianPostWriteUncertain.

The Obsidian bridge (work_buddy.obsidian.bridge) raises
:class:`ObsidianPostWriteUncertain` when a PUT to the plugin times
out client-side after the body has been sent. The vault state may or
may not reflect the change — Obsidian may have processed the write
and just lagged on the response, or the connection may have been
severed before the plugin received anything.

The gateway invokes :func:`verify_post_write` to make the call:

  - "verified"      → the write actually landed; gateway returns
                      success-with-warning to the caller, marks the op
                      completed, and does NOT enqueue a retry. This
                      closes the latent double-write hazard.
  - "absent"        → the write definitively didn't land; gateway
                      enqueues a retry as if the failure had been a
                      plain ObsidianTimeout.
  - "indeterminate" → can't tell (e.g. filesystem read failed). Treat
                      conservatively as "absent" — the retry will
                      re-execute the capability and either succeed
                      (if the original write DID land, the retry's
                      first action is typically a re-read that
                      picks up the new content) or land cleanly.

Read from FILESYSTEM, not the bridge — the bridge is sick by
definition when this verifier runs. Any attempt to round-trip a read
through the bridge would just hit another timeout.

This module has NO dependencies on the bridge module to keep the
import surface clean and avoid cycles.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.obsidian.effects import EffectSpec
from work_buddy.obsidian.errors import ObsidianPostWriteUncertain

logger = get_logger(__name__)

VerifyResult = Literal["verified", "absent", "indeterminate"]
# Multi-effect verdict — adds "partial" for "some effects landed,
# some didn't" (the failure mode that single-effect verify would
# misclassify as "verified" today). Same vocabulary otherwise.
EffectsVerifyResult = Literal["verified", "partial", "absent", "indeterminate"]


def verify_post_write(exc: ObsidianPostWriteUncertain) -> VerifyResult:
    """Verify whether a timed-out write actually landed in the vault.

    Args:
        exc: The exception raised by the bridge. Carries
            ``path`` (vault-relative), ``content_hint`` (substring
            for insert/append modes; ``"sha256:<hex>"`` for replace
            mode), and ``write_mode`` (``"replace" | "insert" | "append"``).

    Returns:
        ``"verified"`` — file exists and content_hint is present.
        ``"absent"``   — file exists but content_hint is missing,
                         OR file doesn't exist.
        ``"indeterminate"`` — couldn't even read the filesystem;
                              caller should treat as absent (retry).

    Logs at INFO level for "verified" and "absent" outcomes (these
    are routine recovery decisions); WARNING for "indeterminate"
    (something went wrong with the verify itself).
    """
    abs_path = _resolve_vault_path(exc.path)
    if abs_path is None:
        logger.warning(
            "post_write_verify: unable to resolve vault root for path=%r",
            exc.path,
        )
        return "indeterminate"

    if not abs_path.exists():
        # File doesn't exist on disk — write definitively didn't land.
        # (Or the user deleted the file between the write and the verify;
        # either way, a retry is the right call.)
        logger.info(
            "post_write_verify: absent (file does not exist) path=%r",
            exc.path,
        )
        return "absent"

    try:
        content = abs_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as read_err:
        logger.warning(
            "post_write_verify: indeterminate (read failed: %s) path=%r",
            read_err, exc.path,
        )
        return "indeterminate"

    hint = exc.content_hint or ""
    if not hint:
        # No hint to match against. Conservative: treat as absent so
        # the retry path runs. This shouldn't normally happen — the
        # bridge always populates a hint — but defensive handling.
        logger.warning(
            "post_write_verify: indeterminate (no content_hint) path=%r",
            exc.path,
        )
        return "indeterminate"

    if exc.write_mode == "replace":
        landed = _verify_replace(content, hint)
    elif exc.write_mode == "absent":
        # Delete-style operation: verified iff the witness is NO
        # LONGER in the file. Used by atomic-delete paths where the
        # hint identifies the content that should be GONE (e.g.
        # ``f"🆔 {task_id}"`` for atomic-delete-line-by-task-id).
        # Without this branch, delete operations using substring
        # semantics get the verdict inverted: a successful delete
        # leaves the witness absent, which "insert"-style verify
        # reads as "didn't land" → spurious retry.
        landed = not _verify_substring(content, hint)
    else:
        # insert / append / anything else with a substring witness.
        landed = _verify_substring(content, hint)

    outcome: VerifyResult = "verified" if landed else "absent"
    logger.info(
        "post_write_verify: %s path=%r write_mode=%r hint_len=%d",
        outcome, exc.path, exc.write_mode, len(hint),
    )
    return outcome


def _verify_replace(content: str, hint: str) -> bool:
    """Replace-mode verify: full sha256 must match.

    ``hint`` is expected in the form ``"sha256:<hex>"`` (set by
    :func:`work_buddy.obsidian.bridge._make_content_hint`). If it's
    in some other shape, treat as absent — better to enqueue a retry
    than to false-positive a verification.
    """
    if not hint.startswith("sha256:"):
        logger.warning(
            "post_write_verify: replace-mode hint missing sha256: prefix (hint=%r)",
            hint[:32],
        )
        return False
    expected = hint[len("sha256:"):]
    actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return actual == expected


def _verify_substring(content: str, hint: str) -> bool:
    """Insert/append-mode verify: substring witness must be present.

    The bridge populates the witness as the first 256 chars of the
    inserted payload (see ``_make_content_hint``). Substring search
    is exact — false negatives are possible if the inserted content
    was further edited by the plugin (e.g. trailing newline
    normalization), but we accept that as the cost of cheap
    verification.
    """
    return hint in content


def verify_post_write_effects(
    effects: list[EffectSpec],
    *,
    params: dict | None = None,
) -> EffectsVerifyResult:
    """Walk a multi-effect manifest, return the overall verdict.

    Closes the multi-effect blind spot in :func:`verify_post_write`
    (single-effect verifier). For capabilities that produce multiple
    external effects (e.g. ``task_create`` writes a note file AND
    appends a master-list line), the gateway's PWU recovery path
    invokes THIS function instead of the single-effect one when the
    capability has a non-empty ``effects`` manifest registered.

    Args:
        effects: The capability's declared effect manifest.
        params: The capability's invocation params (used for path /
            witness template substitution and for the optional
            per-effect resolver to look up generated values from
            the capability's idempotency cache).

    Returns:
        ``"verified"``     — every declared effect is present on disk.
        ``"partial"``      — some effects landed, some didn't. Caller
                             enqueues a retry of the FULL capability;
                             the capability is required to be
                             idempotent under retry (``task_create``'s
                             C.2 cache is the canonical example).
        ``"absent"``       — no effects landed. Same as today's
                             single-effect "absent": enqueue retry.
        ``"indeterminate"``— couldn't resolve effect paths/witnesses
                             (capability cache miss / template values
                             missing). Caller treats as absent
                             (conservative).

    Reads from FILESYSTEM only (same rationale as the single-effect
    verifier — the bridge is sick when this runs).
    """
    if not effects:
        # Empty manifest is a programming error at the registration
        # layer, but be defensive — treat as indeterminate so the
        # caller falls back to single-effect behavior.
        logger.warning("verify_post_write_effects: empty effects list")
        return "indeterminate"

    statuses: list[VerifyResult] = []
    for i, effect in enumerate(effects):
        # Resolve any generated values via the optional resolver.
        generated = effect.call_resolver(params or {})
        if generated is None:
            # Resolver couldn't resolve (cache miss or partial values).
            # That effect is indeterminate.
            statuses.append("indeterminate")
            logger.info(
                "verify_post_write_effects: effect %d/%d resolver "
                "returned None — marking indeterminate",
                i + 1, len(effects),
            )
            continue

        path = effect.resolve_path(params or {}, generated)
        if path is None:
            statuses.append("indeterminate")
            logger.warning(
                "verify_post_write_effects: effect %d/%d path templating "
                "failed (kind=%s, path=%r, path_template=%r)",
                i + 1, len(effects), effect.kind, effect.path,
                effect.path_template,
            )
            continue

        witness = effect.resolve_witness(params or {}, generated)

        statuses.append(_verify_one_effect(
            path=path,
            witness=witness,
            mode=effect.witness_mode,
        ))

    # Aggregate to a single verdict.
    return _aggregate_effect_verdicts(statuses)


def _verify_one_effect(
    *,
    path: str,
    witness: str | None,
    mode: str,
) -> VerifyResult:
    """Verify a single effect by reading the file and checking the witness.

    Mirrors the single-effect ``verify_post_write`` body but as a pure
    function over (path, witness, mode) so it can be reused by the
    multi-effect walker.
    """
    abs_path = _resolve_vault_path(path)
    if abs_path is None:
        return "indeterminate"

    if not abs_path.exists():
        # No file on disk → for "absent" mode, that COUNTS as verified
        # (the absence-of-content test trivially passes for an absent
        # file). For substring/sha256 modes, the write didn't land.
        if mode == "absent":
            return "verified"
        return "absent"

    try:
        content = abs_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "indeterminate"

    # No witness → just confirms the file exists, which it does.
    if witness is None:
        return "verified"

    if mode == "sha256":
        landed = _verify_replace(content, witness)
    elif mode == "absent":
        landed = not _verify_substring(content, witness)
    else:
        # substring / insert / append — substring witness present.
        landed = _verify_substring(content, witness)

    return "verified" if landed else "absent"


def _aggregate_effect_verdicts(
    statuses: list[VerifyResult],
) -> EffectsVerifyResult:
    """Reduce a list of per-effect statuses to a single verdict.

    Decision matrix:
      - all verified                 → "verified"
      - any verified, any other      → "partial"
      - all indeterminate            → "indeterminate"
      - some indeterminate, some absent (no verified) → "absent"
        (conservative — schedule a retry; if it was actually verified,
        the retry will see the same end state and the idempotent
        capability won't double-write)
      - all absent                   → "absent"
    """
    if not statuses:
        return "indeterminate"
    if all(s == "verified" for s in statuses):
        return "verified"
    if any(s == "verified" for s in statuses):
        return "partial"
    if all(s == "indeterminate" for s in statuses):
        return "indeterminate"
    return "absent"


def _resolve_vault_path(vault_relative: str) -> Path | None:
    """Resolve a vault-relative path to an absolute filesystem path.

    Returns None if the vault_root config is missing — the caller
    will treat that as "indeterminate" rather than crashing.

    Path separators are normalized: the bridge stores forward-slash
    paths even on Windows; on disk we need OS-native separators.
    """
    try:
        cfg = load_config()
    except Exception as exc:
        logger.warning("post_write_verify: load_config failed: %s", exc)
        return None

    vault_root_str = cfg.get("vault_root")
    if not vault_root_str:
        return None

    # Normalize separators — bridge stores forward-slash paths; on
    # Windows we want backslashes. Path() handles this transparently
    # if we feed it as a single string with the right separators.
    normalized = vault_relative.replace("\\", "/")
    return Path(vault_root_str) / normalized
