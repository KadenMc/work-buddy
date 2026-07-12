#!/usr/bin/env bash
set -euo pipefail

die() {
  echo "acceptance safety failure: $*" >&2
  exit 2
}

require_hosted_runner() {
  [ "${GITHUB_ACTIONS:-}" = "true" ] || die "GITHUB_ACTIONS is not true"
  [ "${WB_RUNNER_ENVIRONMENT:-}" = "github-hosted" ] || die "runner is not GitHub-hosted"
  [ -n "${RUNNER_TEMP:-}" ] || die "RUNNER_TEMP is empty"
  [ "$(id -u)" -ne 0 ] || die "acceptance must not run as root"
}

resolve_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
}

require_under_runner_temp() {
  python3 - "$RUNNER_TEMP" "$1" <<'PY'
from pathlib import Path
import os
import sys

root = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).expanduser().resolve()
if os.path.commonpath((str(root), str(target))) != str(root) or target == root:
    raise SystemExit(f"unsafe acceptance target outside RUNNER_TEMP: {target}")
print(target)
PY
}

wait_for_url() {
  local url="$1"
  local timeout_seconds="${2:-240}"
  local deadline=$((SECONDS + timeout_seconds))
  local http_code
  while true; do
    http_code="$(curl --silent --output /dev/null --write-out '%{http_code}' "$url" 2>/dev/null || true)"
    case "$http_code" in
      2??|3??) return 0 ;;
      000|"") ;;
      *)
        echo "$url returned HTTP $http_code while waiting for readiness" >&2
        return 1
        ;;
    esac
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "timed out waiting for $url" >&2
      return 1
    fi
    sleep 3
  done
}

unset_external_credentials() {
  unset ANTHROPIC_API_KEY OPENAI_API_KEY TELEGRAM_BOT_TOKEN GOOGLE_APPLICATION_CREDENTIALS
  unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AZURE_CLIENT_SECRET
}

record_runner_facts() {
  evidence_dir="$1"
  mkdir -p "$evidence_dir"
  {
    uname -a
    id
    python3 --version
    node --version
    df -h
    printf 'HOME=%s\nRUNNER_TEMP=%s\nPATH=%s\n' "$HOME" "$RUNNER_TEMP" "$PATH"
  } > "$evidence_dir/runner-facts.txt"
}

assert_ports_free() {
  python3 - <<'PY'
import socket

for port in range(5123, 5128):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SystemExit(f"work-buddy port {port} is not free: {exc}")
PY
}
