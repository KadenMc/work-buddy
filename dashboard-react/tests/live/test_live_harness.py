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


def test_truth_panel_seed_has_reviewable_claims_and_preserves_reseeded_state(harness_env):
    harness_env["WB_LIVE_SCENARIO"] = "truth-panel"
    seeder = LIVE_ROOT / "seeds" / "cowork.py"
    first = run_python([seeder], harness_env)
    assert first.returncode == 0, first.stdout + first.stderr
    manifest_path = Path(harness_env["WB_LIVE_FIXTURE_FILE"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture = manifest["truth_panel"]
    assert len(fixture["claim_ids"]) == 5
    assert fixture["app_path"].endswith(f"document_id={fixture['document_id']}")
    script = """
import base64, json, os, subprocess
from pathlib import Path
from flask import Flask
from work_buddy.cowork import api
from work_buddy.document_kernel.client import DocumentKernelClient
from work_buddy.truth import documents, ydoc_store
from work_buddy.truth.registry import TruthStoreRegistry
fixture = json.loads(Path(os.environ['WB_LIVE_FIXTURE_FILE']).read_text())['truth_panel']
store = TruthStoreRegistry().open_store(fixture['store_id'])
record = documents.get_document(store, fixture['document_id'])
snapshot = ydoc_store.read_snapshot(store, snapshot_sha256=record.ydoc_snapshot_sha256)
head = ydoc_store.current_structured_head(store, document_id=record.id, snapshot_sha256=record.ydoc_snapshot_sha256)
with DocumentKernelClient() as kernel:
    projected = kernel.request({'kind': 'project_markdown', 'snapshotBase64': snapshot,
        'updatesBase64': [], 'expectedBaseStructuredHeadSha256': head})
    assert projected.projection is not None
fidelity_script = "import * as Y from 'yjs'; let raw=''; for await(const chunk of process.stdin) raw+=chunk; const doc=new Y.Doc(); Y.applyUpdate(doc, Buffer.from(raw,'base64')); process.stdout.write(String(doc.getMap('wb-cowork:fidelity').get('schema'))); doc.destroy();"
fidelity = subprocess.run(['node', '--input-type=module', '-e', fidelity_script],
    input=base64.b64encode(snapshot).decode(), cwd='dashboard-react', capture_output=True,
    text=True, timeout=30, check=True,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
assert fidelity.stdout == 'cowork-fidelity/v1'
app = Flask(__name__)
app.config['TESTING'] = True
api.register_routes(app)
url = f"/api/truth/doc/{fixture['document_id']}?store_id={store.store_id}"
with app.test_client() as client:
    response = client.get(url)
    assert response.status_code == 200, response.get_json()
    document = response.get_json()
    assert document['initialization_state'] == 'ready'
    assert document['truth_projection_included'] is True
    assert len(document['expressions']) == 6
    assert sum(item['is_fact'] for item in document['expressions']) == 1
    assert all(item['stale'] is None for item in document['expressions'])
    listed = client.get(f"/api/truth/doc/{fixture['document_id']}/truth?store_id={store.store_id}").get_json()
    claims = {item['claim_id']: item for item in listed['claims']}
    by_case = {case: claims[claim_id] for case, claim_id in fixture['claim_ids'].items()}
    assert by_case['with_evidence']['receipt_count'] == 1
    assert by_case['without_evidence']['receipt_count'] == 0
    assert by_case['confirmed']['is_fact'] is True
    assert by_case['needs_review']['needs_review'] is True
    assert by_case['multiple']['connection_count'] == 2
    assert 'confirm' in by_case['with_evidence']['available_actions']
    detail = client.get(f"/api/truth/doc/{fixture['document_id']}/truth/claims/{fixture['claim_ids']['with_evidence']}?store_id={store.store_id}").get_json()
    assert detail['support']['quarantined_only'] is False
    receipt_path = Path(os.environ['WB_LIVE_ROOT']) / 'evidence/truth-panel/with_evidence.txt'
    assert detail['receipts'][0]['source_locator'] == receipt_path.as_uri()
    assert detail['receipts'][0]['integrity']['state'] == 'valid'
    assert fixture['passages']['with_evidence'] in receipt_path.read_text(encoding='utf-8')
    with store._read_connection() as conn:
        counts = {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in ('documents', 'claims', 'expressions', 'evidence', 'claim_links', 'gestures')}
    print(json.dumps(counts, sort_keys=True))
"""
    observed = run_python(["-c", script], harness_env)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    source = Path(manifest["initialized"]["path"]) / fixture["path"]
    initial_text = source.read_text(encoding="utf-8")
    assert initial_text.index(fixture["passages"]["multiple_second"]) - initial_text.index(
        fixture["passages"]["multiple_first"]
    ) > 1_000
    source.write_text("Edited throwaway source remains intact.\n", encoding="utf-8")
    repeated = run_python([seeder], harness_env)
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
    assert source.read_text(encoding="utf-8") == "Edited throwaway source remains intact.\n"
    observed_again = run_python(["-c", script], harness_env)
    assert observed_again.returncode == 0, observed_again.stdout + observed_again.stderr
    assert observed.stdout.splitlines()[-1] == observed_again.stdout.splitlines()[-1]


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
