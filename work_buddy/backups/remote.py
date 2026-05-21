"""GitHub Releases push for backup snapshots.

Uses the ``gh`` CLI as a subprocess. Two reasons:

- Credentials: ``gh`` manages the user's GitHub auth (browser-based
  OAuth or PAT) in its own credential store. work-buddy never sees
  the token directly.
- Idempotency: ``gh release create`` is idempotent on retry by tag,
  and supports private repos out of the box.

If ``gh`` isn't installed / authenticated, push gracefully no-ops and
writes a failure signal to ``.data/backups/last_run.json`` so the
health-check component surfaces the problem in the Settings tab.

See ``architecture/backups`` for the full subsystem reference.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from work_buddy.backups.local import (
    BACKUP_FILENAME,
    MANUAL_SUFFIX,
    RETENTION,
    parse_snapshot_ts,
)
from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.paths import data_dir

logger = get_logger(__name__)


LAST_RUN_FILENAME = "last_run.json"


# ─── Transient-failure handling ─────────────────────────────────────

# Substrings (lower-cased) that mark a `gh` failure as a transient
# network/DNS fault — worth retrying — rather than a permanent
# misconfiguration. The dominant observed fault is intermittent DNS
# resolution of `uploads.github.com` on corporate networks, which Go
# surfaces as `dial tcp: lookup ...: getaddrinfow: ... no data of the
# requested type was found`.
_NETWORK_ERROR_MARKERS = (
    "dial tcp",
    "lookup ",
    "no such host",
    "getaddrinfo",
    "i/o timeout",
    "operation timed out",
    "connection refused",
    "connection reset",
    "network is unreachable",
    "tls handshake",
    "server misbehaving",
    "could not resolve host",
    "temporary failure in name resolution",
    "no data of the requested type",
)

# `gh` result statuses worth retrying within a single push.
_TRANSIENT_PUSH_STATUSES = frozenset({"gh_network", "gh_timeout"})

# Backoff (seconds) slept between push attempts. With the default
# 3 attempts this is two sleeps — worst case ~60s, comfortably inside
# the hourly cron window.
_PUSH_RETRY_BACKOFF = (15, 45)


# ─── Config ─────────────────────────────────────────────────────────


def get_backup_repo() -> str | None:
    """Return the configured backup repo ``<user>/<repo>`` or None.

    Read from ``backups.github.repo`` in ``config.local.yaml``.
    """
    cfg = load_config()
    backups = cfg.get("backups") or {}
    github = backups.get("github") or {}
    repo = github.get("repo")
    return str(repo) if repo else None


# ─── Push ───────────────────────────────────────────────────────────


def push_snapshot(
    snapshot_dir: Path, *,
    repo: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Upload the tarball in ``snapshot_dir`` to GitHub Releases.

    ``snapshot_dir.name`` becomes the release tag (e.g.
    ``snap-2026-05-11T14-23-00Z``). The release title mirrors the tag.
    Release body includes the manifest snippet for at-a-glance
    introspection.

    Transient network/DNS faults are retried in-process up to
    ``max_attempts`` times with a short backoff. The hourly cron is the
    primary caller: a retry here recovers the *current* snapshot within
    the same run, rather than abandoning it and waiting an hour for the
    next (different) snapshot to be the next off-machine copy. Permanent
    faults (gh missing, unauthenticated) are not retried — they cannot
    be fixed by waiting.

    Args:
        snapshot_dir: Local snapshot directory containing the
            ``work-buddy-backup.tar.gz`` tarball.
        repo: GitHub repo as ``<user>/<repo>``. Defaults to
            ``backups.github.repo`` from config. If still unset,
            returns an "unconfigured" result without attempting
            the push.
        max_attempts: Total push attempts before giving up. The first
            attempt plus ``max_attempts - 1`` retries.

    Returns:
        ``{status, tag, repo, error?, gh_stdout, gh_stderr, ...}``.
        ``status`` is one of ``"ok"`` / ``"unconfigured"`` /
        ``"gh_missing"`` / ``"gh_unauthenticated"`` / ``"gh_not_found"``
        / ``"gh_network"`` / ``"gh_timeout"`` / ``"gh_failed"``. On
        success after a retry, ``recovered_after_attempts`` records
        which attempt landed; on exhausted retries, ``attempts`` records
        the total tried. Always returns a dict — never raises (callers
        expect a result, not an exception, so they can write
        last_run.json).
    """
    repo = repo or get_backup_repo()
    if not repo:
        return {"status": "unconfigured",
                "message": "backups.github.repo not set in config.local.yaml"}

    tarball = snapshot_dir / BACKUP_FILENAME
    if not tarball.exists():
        return {"status": "error",
                "error": f"tarball missing: {tarball}"}

    tag = snapshot_dir.name
    body = _format_release_body(snapshot_dir)

    result: dict[str, Any] = {}
    for attempt in range(1, max_attempts + 1):
        result = _attempt_push(tag, tarball, repo, body)
        if result["status"] == "ok":
            if attempt > 1:
                result["recovered_after_attempts"] = attempt
            return result
        if result["status"] not in _TRANSIENT_PUSH_STATUSES:
            # Permanent fault (auth, gh missing, repo gone) — retrying
            # burns time the hourly cron does not have and cannot help.
            return result
        if attempt < max_attempts:
            backoff = _PUSH_RETRY_BACKOFF[
                min(attempt - 1, len(_PUSH_RETRY_BACKOFF) - 1)
            ]
            logger.warning(
                "remote: push of %s failed transiently (%s); "
                "retry %d/%d in %ds",
                tag, result["status"], attempt + 1, max_attempts, backoff,
            )
            time.sleep(backoff)

    result["attempts"] = max_attempts
    logger.error(
        "remote: push of %s exhausted %d attempts (%s)",
        tag, max_attempts, result.get("status"),
    )
    return result


def _attempt_push(
    tag: str, tarball: Path, repo: str, body: str,
) -> dict[str, Any]:
    """One create-or-upload push attempt.

    ``gh release create`` creates the release object via
    ``api.github.com``, then uploads the asset via
    ``uploads.github.com``. If an earlier attempt created the release
    but its asset upload failed (the common DNS-fault shape — the error
    is on the ``uploads.github.com`` POST), the release exists assetless
    and a re-``create`` errors with "already exists". On that signal we
    fall back to ``gh release upload --clobber``, which attaches (or
    replaces) the asset idempotently — so a retried push converges
    instead of looping on "already exists".
    """
    create = [
        "gh", "release", "create", tag, str(tarball),
        "--repo", repo,
        "--title", tag,
        "--notes", body,
    ]
    res = _run_gh(create, op_label="push", repo=repo, tag=tag)
    if res["status"] == "ok":
        return res
    if "already exists" in (res.get("error") or "").lower():
        upload = [
            "gh", "release", "upload", tag, str(tarball),
            "--clobber",
            "--repo", repo,
        ]
        return _run_gh(upload, op_label="push", repo=repo, tag=tag)
    return res


def list_remote_snapshots(repo: str | None = None) -> list[dict[str, Any]]:
    """List all releases on the backup repo, newest first.

    Returns a list of ``{tag, published_at, url, manual}`` entries.
    Manual snapshots are detected by the ``-manual`` suffix on the tag
    name.

    The timestamp reported is the release's ``publishedAt`` — the
    moment the snapshot was pushed off-machine. The ``gh`` field
    ``createdAt`` is deliberately NOT used: for a release whose tag is
    auto-created against a data-only repo, ``createdAt`` is the tagged
    commit's date, which is *identical* for every release. The
    canonical snapshot time always lives in the ``snap-<isots>`` tag —
    see :func:`work_buddy.backups.local.parse_snapshot_ts`.

    ``url`` is not a ``gh release list --json`` field; it is
    reconstructed from ``repo`` + ``tagName``.

    Returns an empty list if ``gh`` is unavailable or the repo is
    unconfigured — callers should distinguish "no snapshots" from
    "no access" via :func:`probe_gh` if it matters.
    """
    repo = repo or get_backup_repo()
    if not repo:
        return []
    cmd = [
        "gh", "release", "list",
        "--repo", repo,
        "--limit", "200",
        "--json", "tagName,publishedAt",
    ]
    res = _run_gh(cmd, op_label="list", repo=repo, tag=None)
    if res["status"] != "ok":
        return []
    try:
        data = json.loads(res["gh_stdout"])
    except (json.JSONDecodeError, KeyError):
        return []
    out: list[dict[str, Any]] = []
    for r in data:
        tag = r.get("tagName", "")
        if not tag.startswith("snap-"):
            continue
        out.append({
            "tag":          tag,
            "published_at": r.get("publishedAt"),
            "url":          f"https://github.com/{repo}/releases/tag/{tag}",
            "manual":       tag.endswith(MANUAL_SUFFIX),
        })
    # gh release list returns newest first by default.
    return out


def delete_remote_snapshot(tag: str, repo: str | None = None) -> dict[str, Any]:
    """Delete a release + its assets from the backup repo."""
    repo = repo or get_backup_repo()
    if not repo:
        return {"status": "unconfigured"}
    cmd = [
        "gh", "release", "delete", tag,
        "--repo", repo,
        "--yes",
        "--cleanup-tag",  # also delete the underlying git tag
    ]
    return _run_gh(cmd, op_label="delete", repo=repo, tag=tag)


def prune_remote_snapshots(repo: str | None = None) -> dict[str, Any]:
    """Apply the same tiered retention policy to remote releases as
    the local sweep applies to local snapshots.

    Algorithm mirrors :func:`work_buddy.backups.local._prune_snapshots`:
    bucket by hour / day / week / month / year for rolling snapshots,
    apply per-tier caps, keep newest-per-bucket. Manual snapshots
    live in their own ``RETENTION['manual']`` bucket (default 20).

    Each release is bucketed by its *snapshot time*, parsed from the
    ``snap-<isots>`` tag — the same key the local sweep uses. A release
    whose tag does not parse is left untouched (neither kept-set nor
    delete-set), so a malformed tag can never trigger a deletion.

    Returns ``{status, kept, pruned, errors}``.
    """
    repo = repo or get_backup_repo()
    if not repo:
        return {"status": "unconfigured", "kept": [], "pruned": [], "errors": []}

    snapshots = list_remote_snapshots(repo=repo)
    if not snapshots:
        return {"status": "ok", "kept": [], "pruned": [], "errors": []}

    parsed = []
    for s in snapshots:
        ts = parse_snapshot_ts(s["tag"])
        if ts is None:
            continue
        parsed.append({
            "tag":    s["tag"],
            "ts":     ts,
            "manual": s["manual"],
        })
    parsed.sort(key=lambda s: s["ts"], reverse=True)

    rolling = [s for s in parsed if not s["manual"]]
    manual = [s for s in parsed if s["manual"]]

    retain_rolling = _select_rolling_retain_set(rolling)
    retain_manual = {s["tag"] for s in manual[: RETENTION["manual"]]}
    keep = retain_rolling | retain_manual

    pruned: list[str] = []
    errors: list[dict[str, str]] = []
    for s in parsed:
        if s["tag"] in keep:
            continue
        res = delete_remote_snapshot(s["tag"], repo=repo)
        if res["status"] == "ok":
            pruned.append(s["tag"])
        else:
            errors.append({"tag": s["tag"], "error": str(res)})
    return {
        "status": "ok" if not errors else "partial",
        "kept":   sorted(keep),
        "pruned": pruned,
        "errors": errors,
    }


# ─── last_run.json (health-check signal) ───────────────────────────


def write_last_run(payload: dict[str, Any]) -> None:
    """Write the health-check signal file with the result of the
    most recent backup operation.

    The Component health check
    (``work_buddy.health.checks.check_github_backup_freshness``)
    reads this file to surface freshness on the Settings tab. Never
    hits the network on the hot path.
    """
    target = data_dir("backups") / LAST_RUN_FILENAME
    target.write_text(json.dumps(payload, indent=2, sort_keys=True),
                      encoding="utf-8")


def read_last_run() -> dict[str, Any] | None:
    """Read the last_run.json signal file, or None if absent."""
    target = data_dir("backups") / LAST_RUN_FILENAME
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ─── Internals ──────────────────────────────────────────────────────


def _run_gh(
    cmd: list[str], *,
    op_label: str, repo: str, tag: str | None,
) -> dict[str, Any]:
    """Run a ``gh`` subprocess, return a structured result.

    Classifies common failure modes (gh missing, auth failure, network)
    so callers + the health check can surface actionable errors.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return {"status": "gh_missing",
                "error": "gh CLI not installed or not on PATH",
                "op": op_label, "repo": repo, "tag": tag}
    except subprocess.TimeoutExpired:
        return {"status": "gh_timeout",
                "error": f"gh {op_label} timed out after 60s",
                "op": op_label, "repo": repo, "tag": tag}

    if proc.returncode == 0:
        return {"status": "ok",
                "op": op_label, "repo": repo, "tag": tag,
                "gh_stdout": proc.stdout, "gh_stderr": proc.stderr}

    # Classify common failure modes from stderr.
    stderr = (proc.stderr or "").strip()
    lower = stderr.lower()
    if "not logged into" in lower or "authentication required" in lower:
        status = "gh_unauthenticated"
    elif any(marker in lower for marker in _NETWORK_ERROR_MARKERS):
        # Transient network/DNS fault — checked before "not found" so a
        # DNS blip is never misread as a missing repo/release.
        status = "gh_network"
    elif "not found" in lower or "could not find any releases" in lower:
        status = "gh_not_found"
    else:
        status = "gh_failed"
    return {
        "status":    status,
        "error":     stderr,
        "exit_code": proc.returncode,
        "op":        op_label, "repo": repo, "tag": tag,
        "gh_stdout": proc.stdout, "gh_stderr": proc.stderr,
    }


def _format_release_body(snapshot_dir: Path) -> str:
    """Lift the manifest into a Markdown body for the GitHub release.

    Useful so the user can browse to a release in their GitHub UI
    and see at a glance what's in the snapshot — schema versions,
    row counts, commit hash — without downloading the tarball.
    """
    from work_buddy.backups.local import _read_manifest_from_tarball
    tarball = snapshot_dir / BACKUP_FILENAME
    try:
        mf = _read_manifest_from_tarball(tarball)
    except Exception as exc:
        logger.warning("remote: could not read manifest for body: %s", exc)
        return "work-buddy backup snapshot. (manifest unreadable)"
    lines = [
        "work-buddy automated backup snapshot.",
        "",
        f"- **Snapshot ts:** `{mf.snapshot_ts}`",
        f"- **Host:** `{mf.host}`",
        f"- **work-buddy version:** {mf.work_buddy_version or '(unknown)'}",
        f"- **Commit:** `{mf.work_buddy_commit or '(none)'}` "
        f"(`{mf.work_buddy_branch or '(detached)'}`"
        f"{' DIRTY' if mf.work_buddy_dirty else ''})",
        "",
        "**Schema versions:**",
    ]
    for db, v in sorted(mf.schema_versions.items()):
        lines.append(f"- `{db}` → v{v}")
    lines.append("")
    lines.append("**Row counts:**")
    for db, counts in sorted(mf.row_counts.items()):
        total = sum(counts.values())
        lines.append(f"- `{db}`: {total} rows ({len(counts)} tables)")
    return "\n".join(lines)


def _select_rolling_retain_set(rolling: list[dict[str, Any]]) -> set[str]:
    """Same algorithm as local._select_rolling_retain_set but keyed on
    the remote ``tag`` rather than the local ``id``. Sorted newest-first
    on entry.
    """
    if not rolling:
        return set()

    def hour_key(ts):    return ts.strftime("%Y-%m-%d-%H")
    def day_key(ts):     return ts.strftime("%Y-%m-%d")
    def week_key(ts):
        y, w, _ = ts.isocalendar(); return f"{y}-W{w:02d}"
    def month_key(ts):   return ts.strftime("%Y-%m")
    def year_key(ts):    return ts.strftime("%Y")

    tiers = [
        ("hourly",  hour_key,  RETENTION["hourly"]),
        ("daily",   day_key,   RETENTION["daily"]),
        ("weekly",  week_key,  RETENTION["weekly"]),
        ("monthly", month_key, RETENTION["monthly"]),
        ("annual",  year_key,  RETENTION["annual"]),
    ]

    retain: set[str] = set()
    for tier_name, key_fn, cap in tiers:
        seen: list[str] = []
        for snap in rolling:
            bk = key_fn(snap["ts"])
            if bk in seen:
                continue
            if cap >= 0 and len(seen) >= cap:
                break
            seen.append(bk)
            retain.add(snap["tag"])
    return retain


# ─── probe ──────────────────────────────────────────────────────────


GH_AUTH_PATTERN = re.compile(r"Logged in to (\S+) account (\S+)", re.MULTILINE)


def probe_gh() -> dict[str, Any]:
    """Run ``gh auth status`` to classify the user's current GitHub
    CLI state. Used by the Component health check + the setup wizard.

    Returns ``{installed, authenticated, account?, host?, error?}``.
    """
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return {"installed": False, "authenticated": False,
                "error": "gh CLI not installed or not on PATH"}
    except subprocess.TimeoutExpired:
        return {"installed": True, "authenticated": False,
                "error": "gh auth status timed out"}
    # gh auth status writes its useful output to stderr regardless of success.
    output = (proc.stderr or "") + (proc.stdout or "")
    if proc.returncode != 0:
        return {"installed": True, "authenticated": False,
                "error": output.strip()}
    match = GH_AUTH_PATTERN.search(output)
    if not match:
        return {"installed": True, "authenticated": True,
                "host": None, "account": None,
                "raw": output.strip()}
    return {
        "installed":     True,
        "authenticated": True,
        "host":          match.group(1),
        "account":       match.group(2),
    }
