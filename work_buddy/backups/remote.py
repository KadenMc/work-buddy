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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from work_buddy.backups.local import BACKUP_FILENAME, MANUAL_SUFFIX, RETENTION
from work_buddy.config import load_config
from work_buddy.logging_config import get_logger
from work_buddy.paths import data_dir

logger = get_logger(__name__)


LAST_RUN_FILENAME = "last_run.json"


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


def push_snapshot(snapshot_dir: Path, *, repo: str | None = None) -> dict[str, Any]:
    """Upload the tarball in ``snapshot_dir`` to GitHub Releases.

    ``snapshot_dir.name`` becomes the release tag (e.g.
    ``snap-2026-05-11T14-23-00Z``). The release title mirrors the tag.
    Release body includes the manifest snippet for at-a-glance
    introspection.

    Args:
        snapshot_dir: Local snapshot directory containing the
            ``work-buddy-backup.tar.gz`` tarball.
        repo: GitHub repo as ``<user>/<repo>``. Defaults to
            ``backups.github.repo`` from config. If still unset,
            returns an "unconfigured" result without attempting
            the push.

    Returns:
        ``{status, tag, repo, error?, gh_stdout, gh_stderr}``.
        ``status`` is one of ``"ok"`` / ``"unconfigured"`` /
        ``"gh_missing"`` / ``"gh_unauthenticated"`` / ``"gh_failed"``.
        Always returns a dict — never raises (callers expect a
        result, not an exception, so they can write last_run.json).
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
    title = tag
    body = _format_release_body(snapshot_dir)

    # gh release create <tag> <files> --repo <repo> --title <t> --notes <body> --prerelease
    # --prerelease is OFF: these are normal releases. Use --draft=false explicitly.
    cmd = [
        "gh", "release", "create", tag, str(tarball),
        "--repo", repo,
        "--title", title,
        "--notes", body,
    ]
    return _run_gh(cmd, op_label="push", repo=repo, tag=tag)


def list_remote_snapshots(repo: str | None = None) -> list[dict[str, Any]]:
    """List all releases on the backup repo, newest first.

    Returns a list of ``{tag, created_at, size_bytes, url, manual}``
    entries. Manual snapshots are detected by the ``-manual`` suffix
    on the tag name.

    Returns an empty list if ``gh`` is unavailable or the repo is
    unconfigured — callers should distinguish "no snapshots" from
    "no access" via :func:`probe_gh` if it matters.
    """
    repo = repo or get_backup_repo()
    if not repo:
        return []
    # ``url`` is NOT a valid ``gh release list --json`` field
    # (would silently error out and return [] from the fallback —
    # bug fixed 2026-05-11). The release URL is reconstructible
    # from ``repo`` + ``tagName`` so we just synthesize it.
    cmd = [
        "gh", "release", "list",
        "--repo", repo,
        "--limit", "200",
        "--json", "tagName,createdAt",
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
            "tag":        tag,
            "created_at": r.get("createdAt"),
            "url":        f"https://github.com/{repo}/releases/tag/{tag}",
            "manual":     tag.endswith(MANUAL_SUFFIX),
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
        ts = _parse_iso_zulu(s.get("created_at", ""))
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


def _parse_iso_zulu(s: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (gh returns ``2026-05-11T14:23:00Z``).

    Returns None on parse failure.
    """
    if not s:
        return None
    # Python <3.11 needs the Z replaced with +00:00; fromisoformat in
    # 3.11+ accepts Z directly. Be permissive either way.
    s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(s2).astimezone(timezone.utc)
    except ValueError:
        return None


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
