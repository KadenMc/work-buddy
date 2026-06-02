"""Read-only status CLI for shell-level pollers.

Two domains, each with a one-shot ``status`` and a blocking ``wait``:

    python -m work_buddy.statusctl consent status <request_id>
    python -m work_buddy.statusctl consent wait   <request_id> [--timeout N]
    python -m work_buddy.statusctl op      status <operation_id>
    python -m work_buddy.statusctl op      wait   <operation_id> [--timeout N]

The verb may be omitted — ``consent <request_id>`` is shorthand for
``consent status <request_id>``.

Design (see the knowledge unit ``operations/status-cli`` for the rationale):

* **One blocking process, not a respawn-per-poll loop.** ``wait`` does the
  tiered sleep-poll internally, so a ``Monitor`` until-loop runs a single
  command instead of paying Python startup on every tick. This is the
  ``kubectl wait`` / ``aws … wait`` shape.
* **Distinct exit codes** so a shell loop can branch on ``$?`` without
  parsing output:

      0  granted / operation completed
      1  denied  / operation failed
      2  timed out (no decision within the deadline; or request expired)
      3  not found (unknown id)
      4  internal error
      130 interrupted (SIGINT)

  One-shot ``status`` always exits 0 and puts the state in the body (use
  ``wait --timeout 0`` for a single check *with* the full exit-code
  vocabulary).
* **Strictly read-only.** It observes consent/operation state; it never
  mints, consumes, or mutates anything. Acting on a ``granted`` result
  (the retry) goes back through the gateway, which re-checks consent.
* **Cheap startup.** Heavy reads are imported lazily inside handlers; an
  ``op`` query never imports the consent stack and vice-versa.

stdout carries the final result (text line, or JSON with ``--json``);
progress and diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

# Exit codes — the branchable verdict vocabulary.
EXIT_OK = 0          # granted / completed
EXIT_NEGATIVE = 1    # denied / failed
EXIT_TIMEOUT = 2     # no decision within the deadline / expired
EXIT_NOT_FOUND = 3   # unknown id
EXIT_ERROR = 4       # internal error
EXIT_INTERRUPT = 130  # SIGINT

# Per-domain mapping of a resolved state → exit code, and which states are
# terminal for the purpose of a ``wait`` (stop polling and return).
_CONSENT_EXIT = {
    "granted": EXIT_OK,
    "denied": EXIT_NEGATIVE,
    "expired": EXIT_TIMEOUT,
    "not_found": EXIT_NOT_FOUND,
}
_CONSENT_TERMINAL = set(_CONSENT_EXIT)  # pending is the only non-terminal state

_OP_EXIT = {
    "completed": EXIT_OK,
    "failed": EXIT_NEGATIVE,
    "not_found": EXIT_NOT_FOUND,
}
# running/stale are non-terminal (keep waiting); not_found IS terminal — the
# gateway only returns an operation_id after writing the record, so a missing
# record means a bad id, not a not-yet-created one.
_OP_TERMINAL = set(_OP_EXIT)

_DEFAULT_TIMEOUT = 600  # seconds


# ---------------------------------------------------------------------------
# Poll cadence
# ---------------------------------------------------------------------------

def _next_interval(elapsed: float, override: float | None) -> float:
    """Tiered poll interval: responsive early, lighter later.

    Humans usually approve fast, so poll tightly for the first half-minute,
    then back off. ``override`` pins a fixed interval when the caller asks.
    """
    if override is not None:
        return override
    if elapsed < 30:
        return 2.0
    if elapsed < 300:
        return 5.0
    return 15.0


# ---------------------------------------------------------------------------
# Wait loop (shared by both domains)
# ---------------------------------------------------------------------------

def _wait(check, terminal_states, timeout, poll_interval, *, on_poll=None):
    """Poll ``check()`` until it returns a terminal state or the deadline.

    Parameters
    ----------
    check:
        Zero-arg callable returning a status view dict containing ``state``.
    terminal_states:
        States that end the wait immediately.
    timeout:
        Seconds. ``0`` checks exactly once; negative waits indefinitely.
    poll_interval:
        Fixed interval override, or ``None`` for the tiered schedule.
    on_poll:
        Optional callback(view, elapsed) for progress reporting.

    Returns
    -------
    (view, timed_out): the last status view and whether the deadline hit
    before a terminal state.
    """
    start = time.monotonic()
    wait_forever = timeout is not None and timeout < 0
    while True:
        view = check()
        elapsed = time.monotonic() - start
        if on_poll is not None:
            on_poll(view, elapsed)
        if view["state"] in terminal_states:
            return view, False
        # Deadline check (after at least one poll, so timeout=0 = check once).
        if not wait_forever and timeout is not None and elapsed >= timeout:
            return view, True
        interval = _next_interval(elapsed, poll_interval)
        if not wait_forever and timeout is not None:
            remaining = timeout - elapsed
            if remaining <= 0:
                return view, True
            interval = min(interval, remaining)
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Domain handlers
# ---------------------------------------------------------------------------

def _resolve_session(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    import os
    return os.environ.get("WORK_BUDDY_SESSION_ID") or None


def _consent_check(request_id: str, session_id: str | None):
    from work_buddy.consent_status import consent_status
    return lambda: consent_status(request_id, session_id=session_id)


def _op_check(operation_id: str):
    from work_buddy.operations_read import operation_status
    return lambda: operation_status(operation_id)


def _emit(view: dict, *, as_json: bool, exit_code: int) -> int:
    """Write the final result to stdout and return the exit code."""
    if as_json:
        out = dict(view)
        out["exit_code"] = exit_code
        print(json.dumps(out))
    else:
        state = view.get("state", "?")
        extra = ""
        if view.get("operation"):
            extra = f" op={view['operation']}"
        elif view.get("name"):
            extra = f" name={view['name']}"
        ident = view.get("request_id") or view.get("operation_id") or ""
        print(f"{state} {ident}{extra}".rstrip())
    return exit_code


def _run_query(check, exit_map, terminal_states, args) -> int:
    """Shared logic for status (one-shot) and wait (blocking)."""
    as_json = getattr(args, "json", False)
    is_wait = args.mode == "wait"

    if not is_wait:
        # One-shot: always exit 0, state in body.
        view = check()
        return _emit(view, as_json=as_json, exit_code=EXIT_OK)

    def _progress(view, elapsed):
        if args.verbose:
            print(
                f"[{int(elapsed)}s] {view['state']}",
                file=sys.stderr, flush=True,
            )

    view, timed_out = _wait(
        check, terminal_states, args.timeout, args.poll_interval,
        on_poll=_progress,
    )
    if timed_out:
        return _emit(view, as_json=as_json, exit_code=EXIT_TIMEOUT)
    return _emit(view, as_json=as_json, exit_code=exit_map[view["state"]])


def _handle_consent(args) -> int:
    session_id = _resolve_session(args.session)
    if args.mode == "wait" and session_id is None:
        # Without a session the grant cross-check is skipped — a wait would
        # only ever see request-record transitions, which is still correct
        # for responded/denied/expired but misses out-of-band grants. Warn.
        print(
            "warning: no session id (WORK_BUDDY_SESSION_ID unset, --session "
            "not given); out-of-band grants will not be detected.",
            file=sys.stderr,
        )
    check = _consent_check(args.id, session_id)
    return _run_query(check, _CONSENT_EXIT, _CONSENT_TERMINAL, args)


def _handle_op(args) -> int:
    check = _op_check(args.id)
    return _run_query(check, _OP_EXIT, _OP_TERMINAL, args)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _add_wait_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--timeout", type=float, default=_DEFAULT_TIMEOUT,
        help="seconds to wait (default 600; 0 = check once; negative = forever)",
    )
    p.add_argument(
        "--poll-interval", type=float, default=None, dest="poll_interval",
        help="fixed poll interval in seconds (default: tiered 2/5/15s)",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="emit per-poll progress to stderr",
    )


def _build_parser() -> argparse.ArgumentParser:
    # --json on a shared parent (default SUPPRESS so an unset level never
    # overwrites a set one) added to BOTH the top parser and every leaf
    # subparser, so `--json` is accepted before OR after the subcommand —
    # a trailing flag is the natural shell order and must work.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="emit the full status dict as JSON on stdout",
    )

    parser = argparse.ArgumentParser(
        prog="statusctl",
        description="Read-only consent/operation status for shell pollers.",
        parents=[common],
    )
    domains = parser.add_subparsers(dest="domain", required=True)

    for domain, ident_help in (
        ("consent", "consent request id (req_…)"),
        ("op", "operation id (op_…)"),
    ):
        d = domains.add_parser(domain, help=f"{domain} status queries")
        verbs = d.add_subparsers(dest="mode", required=True)

        s = verbs.add_parser("status", parents=[common],
                             help="print current state and exit")
        s.add_argument("id", help=ident_help)
        if domain == "consent":
            s.add_argument("--session", default=None,
                           help="agent session id (default: $WORK_BUDDY_SESSION_ID)")

        w = verbs.add_parser("wait", parents=[common],
                             help="block until resolved or timeout")
        w.add_argument("id", help=ident_help)
        if domain == "consent":
            w.add_argument("--session", default=None,
                           help="agent session id (default: $WORK_BUDDY_SESSION_ID)")
        _add_wait_flags(w)

    return parser


def _preprocess(argv: list[str]) -> list[str]:
    """Allow the verb to be omitted: ``consent <id>`` → ``consent status <id>``.

    Only rewrites when the token after the domain is neither a known verb
    nor an option, so explicit forms and ``--help`` are untouched.
    """
    out = list(argv)
    # Find the domain token (first non-option arg).
    for i, tok in enumerate(out):
        if tok in ("consent", "op"):
            nxt = out[i + 1] if i + 1 < len(out) else None
            if nxt is not None and nxt not in ("status", "wait") and not nxt.startswith("-"):
                out.insert(i + 1, "status")
            break
        if not tok.startswith("-"):
            break  # some other leading positional — leave alone
    return out


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        args = parser.parse_args(_preprocess(raw))
    except SystemExit as exc:  # argparse exits 2 on usage error
        return int(exc.code) if exc.code is not None else EXIT_ERROR

    try:
        if args.domain == "consent":
            return _handle_consent(args)
        if args.domain == "op":
            return _handle_op(args)
        parser.error(f"unknown domain: {args.domain}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPT
    except Exception as exc:  # pragma: no cover — defensive top-level guard
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
