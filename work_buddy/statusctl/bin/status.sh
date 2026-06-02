#!/bin/bash
# work-buddy status command — read-only consent/operation status for
# shell-level pollers (the Monitor tool, bash background loops, cron) that
# cannot speak MCP. Forwards to `python -m work_buddy.statusctl`.
#
# This is a TEMPLATE. The SessionStart hook
# work_buddy/statusctl/install_commands.sh materializes it into
# /tmp/wb/status, substituting %%SESSION%% and %%REPO%%. Do not run the
# template directly; do not edit the generated copy.
#
# Usage:
#   bash /tmp/wb/status consent wait   <request_id> [--timeout 600]
#   bash /tmp/wb/status consent status <request_id>
#   bash /tmp/wb/status op wait   <operation_id> [--timeout 600]
#   bash /tmp/wb/status op status <operation_id>
#   bash /tmp/wb/status --help
#
# The verb may be omitted: `consent <id>` == `consent status <id>`.
#
# Exit codes (branch on $? in a Monitor until-loop):
#   0 granted / operation completed
#   1 denied  / operation failed
#   2 timed out (no decision within the deadline; or request expired)
#   3 not found (unknown id)
#   4 internal error / no interpreter
#   130 interrupted (SIGINT)

# Baked in at generation time so consent queries are session-scoped without
# the caller passing --session.
export WORK_BUDDY_SESSION_ID="%%SESSION%%"
REPO="%%REPO%%"
# Source-tree safety: ensure the package is importable even if not installed.
export PYTHONPATH="${REPO}${PYTHONPATH:+:$PYTHONPATH}"

# Resolve a Python interpreter that can import work-buddy. Resolution order:
# explicit override → a `python`/`python3` on PATH → common conda env paths.
_wb_can_import() { "$1" -c "import work_buddy.statusctl.cli" >/dev/null 2>&1; }

WB_PY=""
if [ -n "$WORK_BUDDY_PYTHON" ] && _wb_can_import "$WORK_BUDDY_PYTHON"; then
    WB_PY="$WORK_BUDDY_PYTHON"
fi
if [ -z "$WB_PY" ]; then
    for cand in python python3; do
        if command -v "$cand" >/dev/null 2>&1 && _wb_can_import "$cand"; then
            WB_PY="$cand"; break
        fi
    done
fi
if [ -z "$WB_PY" ]; then
    for base in "$CONDA_PREFIX" "$HOME/miniforge3" "$HOME/anaconda3" "$HOME/miniconda3"; do
        [ -n "$base" ] || continue
        for exe in "$base/envs/work-buddy/python.exe" "$base/envs/work-buddy/bin/python"; do
            if [ -x "$exe" ] && _wb_can_import "$exe"; then WB_PY="$exe"; break 2; fi
        done
    done
fi
if [ -z "$WB_PY" ]; then
    echo "wb status: no Python with work-buddy installed was found." >&2
    echo "Set WORK_BUDDY_PYTHON to your work-buddy env interpreter." >&2
    exit 4
fi

exec "$WB_PY" -m work_buddy.statusctl "$@"
