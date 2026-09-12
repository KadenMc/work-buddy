"""Seed a disposable Task Workspace world through native domain services."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def required_path(name: str) -> Path:
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'{name} is required')
    return Path(value).resolve()


root = required_path('WB_LIVE_ROOT')
data_root = required_path('WORK_BUDDY_DATA_DIR')
config_root = required_path('WORK_BUDDY_CONFIG_DIR')
host_root = required_path('WB_LIVE_HOST_ROOT')
manifest_path = required_path('WB_LIVE_FIXTURE_FILE')
scenario = os.environ.get('WB_LIVE_SCENARIO', 'browse')
if scenario not in {'browse', 'organization'}:
    raise RuntimeError(f'Unknown Tasks scenario: {scenario}')
if not (root / '.wb-live-harness').is_file():
    raise RuntimeError('refusing to seed outside a marked Tasks live temp root')
if any(root not in item.parents for item in (data_root, config_root, host_root, manifest_path)):
    raise RuntimeError('fixture paths must remain inside the Tasks live temp root')
if os.environ.get('WB_LIVE_BACKEND_PORT') == '5127':
    raise RuntimeError('the Tasks fixture must never use the normal dashboard port')

from work_buddy.tasks.store import TaskStore, default_task_db_path  # noqa: E402
from work_buddy.tasks.project_links import project_database_path, registry_snapshot  # noqa: E402

task_path = default_task_db_path().resolve()
project_path = project_database_path().resolve()
if any(data_root not in path.parents for path in (task_path, project_path)):
    raise RuntimeError('Tasks and Projects databases must be contained by the isolated data root')

if manifest_path.exists():
    existing = json.loads(manifest_path.read_text(encoding='utf-8'))
    if existing.get('format') != 'wb-live-fixture/v1' or existing.get('root') != str(root) or existing.get('app') != 'tasks' or existing.get('scenario') != scenario:
        raise RuntimeError('fixture manifest must describe this Tasks world and scenario')
    if not task_path.is_file() or not project_path.is_file():
        raise RuntimeError('existing Tasks fixture authority is missing; refusing to recreate it')
    print(json.dumps({'ok': True, 'fixture_format': existing['format'], 'reseeded': True}))
    raise SystemExit(0)

from work_buddy.projects import store as projects  # noqa: E402
from work_buddy.tasks import runtime  # noqa: E402
from work_buddy.tasks.documents import TaskDocumentStoreManager  # noqa: E402
from work_buddy.tasks.models import Tag, TaskQuery  # noqa: E402
from work_buddy.tasks.service import TaskApplicationService  # noqa: E402


def project(slug: str, name: str, status: str = 'active', aliases=()) -> int:
    current = next((row for row in (registry_snapshot(project_path) or {}).values() if row['slug'] == slug), None)
    if current is None:
        current = projects.upsert_project(slug, name, status=status, aliases=aliases, author='user')
    return int(current['id'])


project_ids = {
    'work_buddy': project('work-buddy', 'Work Buddy', aliases=[('old-buddy', 'old-buddy')]),
    'ecg': project('ecg-research', 'ECG Research', aliases=[('historical-project', 'historical-project')]),
    'past': project('historical-project', 'Historical project', 'past'),
    'long': project('long-project', 'A very long project name to verify filter wrapping and keyboard selection', 'paused'),
}
store = TaskStore(task_path, project_db_path=project_path)
store.initialize()
state = store.system_state()
if not runtime.is_native_authority_epoch(state.authority_epoch):
    runtime.arm_native_authority_latch(
        task_path, cohort_id='live-tasks', target_authority_epoch='native:live-tasks',
        cutover_receipt_id='live-tasks-cutover', armed_at='2026-09-01T09:00:00+00:00',
    )
    store.set_system_state(
        expected_authority_epoch=state.authority_epoch, authority_epoch='native:live-tasks',
        updated_at='2026-09-01T09:00:00+00:00', cutover_receipt_id='live-tasks-cutover', process_generation=1,
    )
document_store = TaskDocumentStoreManager().ensure()
clock = [datetime(2026, 9, 1, 10, tzinfo=timezone.utc)]
service = TaskApplicationService(store, clock=lambda: clock[0])
task_ids: dict[str, str] = {}


def create(key: str, title: str, *, namespaces=(), links=(), attention='active', status='open', **fields):
    task_id = f't-live-{key}'
    task_ids[key] = task_id
    if store.get(task_id, include_deleted=True) is not None:
        return
    clock[0] += timedelta(hours=4)
    task = service.create(
        description=title, task_id=task_id, state='active' if attention == 'snoozed' else attention,
        project_ids=links, tags=[Tag(value, True) for value in namespaces],
        summary_text='Seeded handoff context for the isolated Tasks walkthrough.',
        client_mutation_id=f'live-create:{key}', actor='dashboard:live-seed', **fields,
    ).task
    def mutate(method, suffix, **kwargs):
        nonlocal task
        clock[0] += timedelta(minutes=5)
        task = method(task_id, expected_revision=task.revision, client_mutation_id=f'live-{suffix}:{key}', actor='dashboard:live-seed', **kwargs).task
    if attention == 'snoozed':
        mutate(service.snooze, 'snooze', until='2050-01-01')
    if status in {'completed', 'archived', 'trash'}:
        mutate(service.complete, 'complete')
    if status in {'archived', 'trash'}:
        mutate(service.archive, 'archive')
    if status == 'trash':
        mutate(service.delete, 'delete')


create('shared', 'Improve task browsing', namespaces=['projects/work-buddy/ui', 'research/analysis'], links=[project_ids['work_buddy'], project_ids['ecg']], urgency='high', due_date='2026-09-30')
create('existing-root', 'Check the existing destination namespace', namespaces=['work-buddy'], links=[project_ids['ecg']])
create('legacy-root', 'Plan the namespace cleanup', namespaces=['projects/work-buddy'], links=[project_ids['work_buddy']])
create('direct-parent', 'Resolve tasks directly in the grouping namespace', namespaces=['projects'], links=[project_ids['long']])
create('nested', 'Review deep namespace analysis', namespaces=['projects/ecg/deep/analysis', 'research'], links=[project_ids['ecg']], attention='mit')
create('waiting', 'Waiting for collaborator review', namespaces=['research/review'], links=[project_ids['ecg']], attention='waiting')
create('snoozed', 'Return to this idea later', namespaces=['ideas/parked'], attention='snoozed')
create('focused', 'Write the bounded results section', namespaces=['paper/draft'], links=[project_ids['ecg']], attention='focused')
create('completed', 'Completed comparison analysis', namespaces=['projects/work-buddy'], links=[project_ids['work_buddy']], status='completed')
create('archived', 'Archived namespace membership', namespaces=['projects/work-buddy/archive'], links=[project_ids['work_buddy']], status='archived')
create('trash', 'Trashed namespace membership', namespaces=['projects/work-buddy/trash'], links=[project_ids['work_buddy']], status='trash')
create('unassigned', 'Task without namespace or project', attention='inbox')
create('project-only', 'Project association without namespace', links=[project_ids['work_buddy']])
create('unresolved', 'Resolve an unmatched historical project', namespaces=['personal/admin'], project='unknown-project')
create('ambiguous', 'Resolve an ambiguous historical project', project='historical-project')
create('long', 'Review a deliberately long task title and a deeply nested namespace without clipping the controls or dates', namespaces=['research/very-long-namespace-name/long-subtree-name/analysis'], links=[project_ids['long']])
create('newest', 'Recently captured task', attention='inbox', namespaces=['inbox/personal'])

# A separate page of older records exposes global ordering and pagination.
# Batch creation retains production receipts/history/outbox without per-row setup.
clock[0] = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
service.batch_create([
    {'task_id': f't-live-page-{index:03d}', 'description': f'Earlier task {index:03d}',
     'state': 'active', 'namespaces': ['backlog/earlier'], 'urgency': ['low', 'medium', 'high'][index % 3]}
    for index in range(125)
], client_mutation_id='live-pages-v1', actor='dashboard:live-seed')

sentinel = host_root / 'isolation-sentinel.txt'
sentinel.write_bytes(b'wb-live-isolated-tasks\n')
payload = {
    'format': 'wb-live-fixture/v1', 'app': 'tasks', 'scenario': scenario,
    'root': str(root), 'host_root': str(host_root),
    'sentinel': {'path': str(sentinel), 'sha256': hashlib.sha256(sentinel.read_bytes()).hexdigest()},
    'tasks': {'task_ids': task_ids, 'project_ids': project_ids, 'task_db_path': str(task_path),
              'project_db_path': str(project_path), 'task_count': len(store.list(TaskQuery(include_done=True, include_archived=True, include_deleted=True, include_snoozed=True, limit=1000))),
              'app_path': '/app/tasks', 'document_store_id': document_store.store_id,
              'namespace_source': 'projects', 'collision_destination': 'work-buddy',
              'page_rows': 125},
}
manifest_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
print(json.dumps({'ok': True, 'fixture_format': payload['format'], 'task_count': payload['tasks']['task_count']}))
