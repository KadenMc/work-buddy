"""Exercise live-host refusal and seeder idempotence on disposable roots."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
LIVE_ROOT = REPO_ROOT / "dashboard-react" / "tests" / "live"


@pytest.fixture
def harness_env(tmp_path):
    root = tmp_path / "harness"
    root.mkdir()
    for child in ("data", "config", "host-folders"):
        (root / child).mkdir()
    (root / ".wb-live-harness").write_text("wb-live-harness/v1\n", encoding="utf-8")
    (root / "config" / "config.yaml").write_text(json.dumps({
        "vault_root": str(root / "host-folders"),
        "paths": {"data_root": str(root / "data")},
        "dashboard": {"cowork_allowed_roots": [str(root / "host-folders")]},
    }), encoding="utf-8")
    return {
        **os.environ,
        "WORK_BUDDY_CONFIG_DIR": str(root / "config"),
        "WORK_BUDDY_DATA_DIR": str(root / "data"),
        "WORK_BUDDY_ASSET_ROOT": str(REPO_ROOT),
        "WORK_BUDDY_SESSION_ID": "isolated-live-harness-tests",
        "WB_LIVE_ROOT": str(root),
        "WB_LIVE_HOST_ROOT": str(root / "host-folders"),
        "WB_LIVE_FIXTURE_FILE": str(root / "fixture.json"),
        "WB_LIVE_BACKEND_PORT": "54321",
        "WB_LIVE_HARNESS_NONCE": "isolated-fixture-nonce",
    }


def run_python(args, env):
    return subprocess.run(
        ["uv", "run", "--no-sync", "python", *map(str, args)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


@pytest.mark.parametrize("guard", ["marker", "containment", "port"])
def test_live_server_refuses_unsafe_start(harness_env, tmp_path, guard):
    root = Path(harness_env["WB_LIVE_ROOT"])
    if guard == "marker":
        (root / ".wb-live-harness").unlink()
        expected = "marked live temp root"
    elif guard == "containment":
        harness_env["WORK_BUDDY_DATA_DIR"] = str(tmp_path / "outside")
        expected = "must be contained"
    else:
        harness_env["WB_LIVE_BACKEND_PORT"] = "5127"
        expected = "never use the normal dashboard port"
    result = run_python([LIVE_ROOT / "live_server.py"], harness_env)
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (tmp_path / "outside").exists()


def test_co_work_seeds_one_world_without_overwriting_existing_writing(harness_env):
    seeder = LIVE_ROOT / "seeds" / "cowork.py"
    first = run_python([seeder], harness_env)
    assert first.returncode == 0, first.stderr
    manifest_path = Path(harness_env["WB_LIVE_FIXTURE_FILE"])
    first_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_manifest["harness"] = {"nonce": "retain-runner-metadata"}
    manifest_path.write_text(json.dumps(first_manifest), encoding="utf-8")
    source = Path(first_manifest["source"]["path"])
    source.write_text("Edited during an isolated walkthrough.\n", encoding="utf-8")
    second = run_python([seeder], harness_env)
    assert second.returncode == 0, second.stderr
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == first_manifest
    assert source.read_text(encoding="utf-8") == "Edited during an isolated walkthrough.\n"
    count = run_python([
        "-c", "from work_buddy.truth.registry import TruthStoreRegistry; "
        "print(len(TruthStoreRegistry().list_stores()))",
    ], harness_env)
    assert count.returncode == 0, count.stderr
    assert count.stdout.strip() == "1"


def test_seed_manifest_must_remain_inside_marked_root(harness_env, tmp_path):
    harness_env["WB_LIVE_FIXTURE_FILE"] = str(tmp_path / "outside.json")
    result = run_python([LIVE_ROOT / "seeds" / "cowork.py"], harness_env)
    assert result.returncode != 0
    assert "must remain inside" in result.stderr
    assert not (tmp_path / "outside.json").exists()


def test_harness_identity_requires_nonce_and_exact_origin_and_redeems_once(harness_env):
    script = """
import runpy
namespace = runpy.run_path('dashboard-react/tests/live/live_server.py')
app = namespace['app']
origin = 'http://127.0.0.1:54321'
headers = {'Origin': origin, 'X-WB-Live-Control': 'isolated-fixture-nonce'}
with app.test_client() as client:
    denied = client.post('/api/_live/identity-bootstrap', base_url=origin,
                         json={'origin': origin})
    assert denied.status_code == 403
    mismatch = client.post('/api/_live/identity-bootstrap', base_url=origin,
                           headers=headers, json={'origin': 'http://127.0.0.1:54322'})
    assert mismatch.status_code == 403
    minted = client.post('/api/_live/identity-bootstrap', base_url=origin,
                         headers=headers, json={'origin': origin})
    assert minted.status_code == 200, minted.get_json()
    assert minted.headers['X-WB-Live-Harness'] == 'isolated-fixture-nonce'
    grant = minted.get_json()
    redeemed = client.post('/api/local-identity/bootstrap/redeem', base_url=origin,
                           headers={'Origin': origin}, json={'token': grant['token']})
    assert redeemed.status_code == 200, redeemed.get_json()
    assert redeemed.get_json()['authenticated'] is True
    replay = client.post('/api/local-identity/bootstrap/redeem', base_url=origin,
                         headers={'Origin': origin}, json={'token': grant['token']})
    assert replay.status_code != 200
"""
    result = run_python(["-c", script], harness_env)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_pickers_use_only_declared_fixtures_and_cannot_launch_native_ui(harness_env):
    seeded = run_python([LIVE_ROOT / "seeds" / "cowork.py"], harness_env)
    assert seeded.returncode == 0, seeded.stderr
    script = """
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

namespace = runpy.run_path('dashboard-react/tests/live/live_server.py')
app = namespace['app']
fixture_path = Path(os.environ['WB_LIVE_FIXTURE_FILE'])
fixture = json.loads(fixture_path.read_text(encoding='utf-8'))
from work_buddy.cowork import native_folder_chooser
from work_buddy.cowork.project_store import ProjectStoreManager
from work_buddy.truth.registry import TruthStoreRegistry
manager = ProjectStoreManager()
inspection = manager.inspect(fixture['ordinary']['path'])
ordinary_store = manager.initialize(fixture['ordinary']['path'], registry=TruthStoreRegistry(),
    inspection_fingerprint=inspection.fingerprint, idempotency_key='picker-route-fixture')
origin = 'http://127.0.0.1:54321'
headers = {'Origin': origin, 'X-WB-Live-Control': 'isolated-fixture-nonce'}
with patch('subprocess.run', side_effect=AssertionError('Picker attempted a native subprocess')):
  with app.test_client() as client:
    mint = client.post('/api/_live/identity-bootstrap', base_url=origin,
        headers=headers, json={'origin': origin})
    assert mint.status_code == 200, mint.get_json()
    redeemed = client.post('/api/local-identity/bootstrap/redeem', base_url=origin,
        headers={'Origin': origin}, json={'token': mint.get_json()['token']})
    assert redeemed.status_code == 200, redeemed.get_json()
    def pick(route, intent, store_id=None):
        result = client.post('/api/truth/cowork/' + route, base_url=origin,
            headers={'Origin': origin, 'X-Work-Buddy-Intent': intent},
            json={'store_id': store_id} if store_id else {})
        assert result.status_code == 200, result.get_json()
        return result.get_json()
    chosen = pick('folders/choose', 'cowork-folder-picker')
    assert chosen['cancelled'] is False
    assert Path(chosen['folder_path']) == Path(fixture['initialized']['path'])
    assert chosen['selection_token']
    for route, intent in [('files/choose-import', 'cowork-import-picker'),
                          ('files/choose-markdown', 'cowork-markdown-picker')]:
        imported = pick(route, intent, ordinary_store.store_id)
        assert imported['cancelled'] is False
        assert imported['path'] == fixture['source']['relative_path']
        assert pick(route, intent, fixture['initialized']['store_id'])['cancelled'] is True
    location = pick('folders/choose-location', 'cowork-location-picker', fixture['initialized']['store_id'])
    assert location['cancelled'] is False and location['path'] == ''
    try:
        native_folder_chooser._run_dialog([sys.executable, '-m', 'work_buddy.cowork.folder_picker_helper'])
    except native_folder_chooser.NativeFolderChooserError as exc:
        assert exc.code == 'harness_native_dialog_forbidden'
    else:
        raise AssertionError('Native picker adapter did not refuse')

# Exercise the process audit boundary directly; it rejects before OS spawn.
try:
    subprocess.run([sys.executable, '-m', 'work_buddy.cowork.folder_picker_helper'],
        capture_output=True, timeout=1,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
except PermissionError as exc:
    assert 'Native picker processes are disabled' in str(exc)
else:
    raise AssertionError('Native picker process was not refused')
for command in [['/usr/bin/osascript', '-e', 'choose folder'], ['zenity', '--file-selection'],
                '"C:/fixture tools/zenity.exe" --file-selection']:
    try:
        sys.audit('subprocess.Popen', None, command, None, None)
    except PermissionError:
        pass
    else:
        raise AssertionError('Platform picker process was not refused')

try:
    namespace['_choose_fixture_location'](Path(os.environ['WB_LIVE_ROOT']).parent)
except RuntimeError as exc:
    assert 'contained by the fixture host root' in str(exc)
else:
    raise AssertionError('Picker admitted a path outside the fixture root')
fixture['initialized']['path'] = str(Path(os.environ['WB_LIVE_ROOT']).parent)
fixture_path.write_text(json.dumps(fixture), encoding='utf-8')
try:
    namespace['_choose_fixture_folder']()
except RuntimeError as exc:
    assert 'contained by the fixture host root' in str(exc)
else:
    raise AssertionError('Picker admitted an escaping manifest selection')
(Path(os.environ['WB_LIVE_ROOT']) / '.wb-live-harness').unlink()
try:
    namespace['_choose_fixture_location'](fixture['ordinary']['path'])
except RuntimeError as exc:
    assert 'marked temp root' in str(exc)
else:
    raise AssertionError('Picker ignored a removed marker')
"""
    result = run_python(["-c", script], harness_env)
    assert result.returncode == 0, result.stdout + result.stderr
