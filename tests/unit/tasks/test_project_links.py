from __future__ import annotations

import json
import sqlite3

import pytest

from work_buddy.tasks.errors import TaskIdempotencyConflict, TaskRevisionConflict, TaskValidationError
from work_buddy.tasks.migrations import NativeTaskMigrationRunner, TASK_MIGRATIONS
from work_buddy.tasks.models import Tag, Task, TaskQuery
from work_buddy.tasks.service import TaskApplicationService
from work_buddy.tasks.store import TaskStore

from .conftest import create_task, legacy_project_receipt


def registry(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE projects(id INTEGER PRIMARY KEY,slug TEXT,name TEXT,status TEXT);
        CREATE TABLE project_aliases(project_id INTEGER,alias_norm TEXT);
        INSERT INTO projects VALUES(1,'work-buddy','Work Buddy','active'),
            (2,'ecg','ECG Research','paused'),(3,'retired','Retired project','deleted');
        INSERT INTO project_aliases VALUES(1,'old-buddy'),(1,'ambiguous'),(2,'ambiguous');
    """)
    conn.close()
    return path


@pytest.fixture
def project_service(task_service, task_store, tmp_path):
    task_store.project_db_path = registry(tmp_path / 'projects.db')
    return task_service


def authority(task, mutation):
    return dict(expected_revision=task.revision, client_mutation_id=mutation, actor='dashboard:user')


def test_multiple_links_and_namespaces_are_independent_with_receipt_replay(project_service, task_store):
    created = create_task(project_service, project_ids=[2, 1, 2], tags=[Tag('projects/unregistered/deep', True)])
    assert created.task.project_ids == (1, 2)
    assert created.task.project is None
    assert not created.task.unresolved_projects
    assert Task.from_dict(created.task.to_dict()) == created.task
    changed = project_service.update(created.task.task_id, changes={}, tags=[Tag('work-buddy', True)], **authority(created.task, 'flatten'))
    assert changed.task.project_ids == (1, 2)
    assert changed.task.namespace_tags == ('work-buddy',)
    replay = project_service.update(created.task.task_id, changes={}, tags=[Tag('work-buddy', True)], **authority(created.task, 'flatten'))
    assert replay.replayed
    assert replay.task.project_ids == (1, 2)
    assert len(task_store.history(created.task.task_id)) == 2
    assert len(task_store.pending_outbox()) == 2
    assert [task.task_id for task in task_store.list(TaskQuery(project='old-buddy'))] == [created.task.task_id]
    assert [task.task_id for task in task_store.list(TaskQuery(project='2'))] == [created.task.task_id]


def test_explicit_legacy_project_resolves_without_adding_namespace(project_service):
    task = create_task(project_service, project='old-buddy', tags=[Tag('research', True)]).task
    assert task.project_ids == (1,)
    assert task.namespace_tags == ('research',)
    assert not task.unresolved_projects


def test_ambiguous_ingress_can_be_resolved_explicitly_without_touching_namespaces(project_service):
    task = create_task(project_service, project='ambiguous', tags=[Tag('lab/analysis', True)]).task
    assert task.project_ids == ()
    assert task.unresolved_projects[0]['candidate_ids'] == [1, 2]
    assert task.unresolved_projects[0]['reason'] == 'ambiguous'
    updated = project_service.update(task.task_id, changes={}, project_ids=[2], remove_unresolved_projects=['ambiguous'], **authority(task, 'resolve'))
    assert updated.task.project_ids == (2,)
    assert updated.task.unresolved_projects == ()
    assert updated.task.namespace_tags == task.namespace_tags
    with pytest.raises(TaskRevisionConflict):
        project_service.update(task.task_id, changes={}, project_ids=[1], **authority(task, 'stale'))


def test_registry_failure_preserves_existing_and_unmatched_links(project_service, task_store):
    task = create_task(project_service, project_ids=[1], project='unknown').task
    task_store.project_db_path.unlink()
    updated = project_service.update(task.task_id, changes={'description': 'Still editable'}, project_ids=[1], **authority(task, 'keep')).task
    assert updated.project_ids == (1,)
    assert updated.unresolved_projects[0]['legacy_value'] == 'unknown'
    with pytest.raises(TaskValidationError) as error:
        project_service.update(task.task_id, changes={}, project_ids=[1, 2], **authority(updated, 'add'))
    assert 'registry is unavailable' in error.value.field_errors['project_ids']
    assert not task_store.project_db_path.exists()
    replay = create_task(project_service, project_ids=[1], project='unknown')
    assert replay.replayed
    assert replay.task.project_ids == (1,)


def test_project_rename_and_deleted_project_do_not_break_identity(project_service, task_store):
    task = create_task(project_service, project_ids=[1, 3]).task
    conn = sqlite3.connect(task_store.project_db_path)
    conn.execute("UPDATE projects SET slug='renamed' WHERE id=1")
    conn.commit()
    conn.close()
    assert task_store.get(task.task_id).project_ids == (1, 3)
    assert task_store.list(TaskQuery(project='renamed'))[0].task_id == task.task_id


@pytest.mark.parametrize('numeric_name_kind', ['slug', 'alias'])
def test_legacy_project_query_prefers_numeric_names_over_registry_ids(project_service, task_store, numeric_name_kind):
    conn = sqlite3.connect(task_store.project_db_path)
    conn.execute("INSERT INTO projects VALUES(7,?,'Numeric project','active')", ('2' if numeric_name_kind == 'slug' else 'numeric-name',))
    if numeric_name_kind == 'alias':
        conn.execute("INSERT INTO project_aliases VALUES(7,'2')")
    conn.commit()
    conn.close()
    named = create_task(project_service, task_id='t-numeric-name', mutation_id='numeric-name', project_ids=[7]).task
    create_task(project_service, task_id='t-numeric-id', mutation_id='numeric-id', project_ids=[2])
    fallback = create_task(project_service, task_id='t-fallback', mutation_id='fallback', project_ids=[3]).task
    assert [task.task_id for task in task_store.list(TaskQuery(project='2'))] == [named.task_id]
    assert [task.task_id for task in task_store.list(TaskQuery(project='3'))] == [fallback.task_id]


def test_batch_creates_multiple_links_and_never_generates_project_namespaces(project_service):
    result = project_service.batch_create([
        {'description': 'Shared', 'project_ids': [1, 2], 'namespaces': ['shared']},
        {'description': 'Unresolved', 'project': 'unknown'},
    ], client_mutation_id='batch-projects', actor='dashboard:user')
    assert result.tasks[0].project_ids == (1, 2)
    assert result.tasks[0].namespace_tags == ('shared',)
    assert result.tasks[1].unresolved_projects[0]['legacy_value'] == 'unknown'
    assert result.tasks[1].namespace_tags == ()


def legacy_store(path):
    conn = sqlite3.connect(path, isolation_level=None)
    runner = NativeTaskMigrationRunner('task_metadata', migrations=[item for item in TASK_MIGRATIONS.migrations if item.version <= 22])
    runner.run(conn)
    conn.execute("INSERT INTO task_metadata(task_id,description,state,urgency,created_at,updated_at,deleted_at) VALUES('t-old','Historical','done','medium','2025-01-01','2025-02-01','2025-03-01')")
    conn.executemany("INSERT INTO task_tags(task_id,tag,is_namespace) VALUES('t-old',?,1)", [(value,) for value in ['projects/old-buddy/deep/path', 'projects/ecg', 'projects/unknown/keep', 'projects/ambiguous', 'personal']])
    conn.close()


def test_migration_preserves_every_historical_association_path_and_ambiguity(tmp_path):
    path = tmp_path / 'tasks.db'
    legacy_store(path)
    store = TaskStore(path, project_db_path=registry(tmp_path / 'projects.db'))
    task = store.get('t-old', include_deleted=True)
    assert task.project_ids == (1, 2)
    assert {item['legacy_value']: item['reason'] for item in task.unresolved_projects} == {'ambiguous': 'ambiguous', 'unknown': 'unmatched'}
    assert 'projects/old-buddy/deep/path' in task.namespace_tags
    assert task.deleted_at == '2025-03-01'
    assert task.created_at == '2025-01-01'
    conn = store.connect()
    assert conn.execute('SELECT COUNT(*) FROM task_project_migration_log').fetchone()[0] == 4
    # Future tags with an identical spelling remain ordinary namespaces.
    conn.execute("INSERT INTO task_tags(task_id,tag,is_namespace) VALUES('t-old','projects/retired',1)")
    conn.close()
    assert store.get('t-old', include_deleted=True).project_ids == (1, 2)


def test_migration_can_finish_after_registry_unavailability_without_losing_sources(tmp_path):
    path = tmp_path / 'tasks.db'
    legacy_store(path)
    store = TaskStore(path, project_db_path=tmp_path / 'projects.db')
    pending = store.get('t-old', include_deleted=True)
    assert len(pending.unresolved_projects) == 4
    assert all(item['reason'] == 'pending' for item in pending.unresolved_projects)
    assert not store.project_db_path.exists()
    registry(store.project_db_path)
    finished = store.get('t-old', include_deleted=True)
    assert finished.project_ids == (1, 2)
    assert len(finished.unresolved_projects) == 2
    assert finished.revision == pending.revision + 1
    assert finished.updated_at != pending.updated_at
    assert store.collection_revision() == 1
    history = store.history('t-old')
    outbox = store.pending_outbox()
    assert len(history) == len(outbox) == 1
    assert history[0].mutation == outbox[0]['mutation'] == 'project.backfill'
    assert history[0].task_revision == outbox[0]['task_revision'] == finished.revision
    assert history[0].details['project_ids'] == {'before': [], 'after': [1, 2]}
    # Completed decisions and a repeated schema/connection initialization are
    # inert, even when a previously unmatched alias later becomes resolvable.
    conn = sqlite3.connect(store.project_db_path)
    conn.execute("INSERT INTO project_aliases VALUES(1,'unknown')")
    conn.commit()
    conn.close()
    store.initialize()
    store.initialize()
    assert store.get('t-old', include_deleted=True) == finished
    assert store.collection_revision() == 1
    assert len(store.history('t-old')) == len(store.pending_outbox()) == 1
    conn = store.connect()
    receipt = conn.execute('SELECT * FROM task_mutation_receipts').fetchone()
    assert receipt['status'] == 'completed'
    assert json.loads(receipt['result_json'])['task']['project_ids'] == [1, 2]
    assert conn.execute('SELECT COUNT(*) FROM task_mutation_receipts').fetchone()[0] == 1
    conn.close()


def test_deferred_backfill_rejects_stale_save_without_dropping_resolved_projects(tmp_path):
    from work_buddy.tasks import runtime

    path = tmp_path / 'tasks.db'
    legacy_store(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE task_metadata SET state='inbox',deleted_at=NULL WHERE task_id='t-old'")
    conn.commit()
    conn.close()
    store = TaskStore(path, project_db_path=tmp_path / 'projects.db')
    pending = store.get('t-old')
    assert pending.project_ids == ()
    runtime.arm_native_authority_latch(
        path, cohort_id='unit-test', target_authority_epoch='native:unit-test',
        cutover_receipt_id='unit-test-cutover', armed_at='2026-08-23T16:59:59+00:00',
    )
    store.set_system_state(
        expected_authority_epoch=store.system_state().authority_epoch,
        authority_epoch='native:unit-test', updated_at='2026-08-23T17:00:00+00:00',
        cutover_receipt_id='unit-test-cutover', process_generation=1,
    )
    registry(store.project_db_path)
    # The next connection resolves pending links before Save checks the draft's
    # revision. An explicit empty selection from that old draft must not win.
    with pytest.raises(TaskRevisionConflict):
        TaskApplicationService(store).update(
            pending.task_id, changes={'description': 'Stale form title'},
            project_ids=list(pending.project_ids), **authority(pending, 'stale-project-save'),
        )
    current = store.get(pending.task_id)
    assert current.project_ids == (1, 2)
    assert current.description == pending.description
    assert current.namespace_tags == pending.namespace_tags
    assert current.revision == pending.revision + 1
    assert store.collection_revision() == 1
    assert len(store.history(pending.task_id)) == len(store.pending_outbox()) == 1


def test_backfill_audit_failure_rolls_back_and_reentry_applies_once(tmp_path):
    path = tmp_path / 'tasks.db'
    legacy_store(path)
    store = TaskStore(path, project_db_path=tmp_path / 'projects.db')
    pending = store.get('t-old', include_deleted=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TRIGGER reject_backfill BEFORE INSERT ON task_event_outbox "
                 "WHEN NEW.mutation='project.backfill' BEGIN SELECT RAISE(ABORT,'outbox unavailable'); END")
    conn.commit()
    registry(store.project_db_path)
    with pytest.raises(sqlite3.IntegrityError, match='outbox unavailable'):
        store.get('t-old', include_deleted=True)
    assert conn.execute("SELECT revision FROM task_metadata WHERE task_id='t-old'").fetchone()[0] == pending.revision
    assert conn.execute("SELECT COUNT(*) FROM task_project_unresolved WHERE reason='pending'").fetchone()[0] == 4
    assert conn.execute('SELECT COUNT(*) FROM task_projects').fetchone()[0] == 0
    assert conn.execute('SELECT revision FROM task_collection_state').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM task_mutation_receipts').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM task_state_history').fetchone()[0] == 0
    conn.execute('DROP TRIGGER reject_backfill')
    conn.commit()
    conn.close()
    finished = store.get('t-old', include_deleted=True)
    assert finished.project_ids == (1, 2)
    assert finished.revision == pending.revision + 1
    assert store.get('t-old', include_deleted=True) == finished
    assert len(store.history('t-old')) == len(store.pending_outbox()) == 1


@pytest.mark.parametrize('registry_change', ['appears', 'disappears', 'alias_changes'])
def test_legacy_project_batch_receipt_replay_survives_registry_changes(task_service, task_store, tmp_path, registry_change):
    task_store.project_db_path = tmp_path / 'projects.db'
    if registry_change != 'appears':
        registry(task_store.project_db_path)
    items = [{'description': 'Legacy project batch', 'project': 'old-buddy', 'namespaces': ['independent']}]
    original = task_service.batch_create(items, client_mutation_id='legacy-batch', actor='dashboard:user')
    if registry_change == 'appears':
        assert original.tasks[0].unresolved_projects[0]['reason'] == 'registry_unavailable'
        registry(task_store.project_db_path)
    elif registry_change == 'disappears':
        task_store.project_db_path.unlink()
    else:
        conn = sqlite3.connect(task_store.project_db_path)
        conn.execute("UPDATE project_aliases SET project_id=2 WHERE alias_norm='old-buddy'")
        conn.commit()
        conn.close()
    replay = task_service.batch_create(items, client_mutation_id='legacy-batch', actor='dashboard:user')
    assert replay.replayed
    assert replay.tasks == original.tasks
    assert replay.receipt == original.receipt
    assert replay.collection_revision == original.collection_revision
    assert len(task_store.list()) == 1
    assert len(task_store.history(original.tasks[0].task_id)) == len(task_store.pending_outbox()) == 1
    with pytest.raises(TaskIdempotencyConflict):
        task_service.batch_create([{**items[0], 'project': 'ecg'}], client_mutation_id='legacy-batch', actor='dashboard:user')


@pytest.mark.parametrize('project', [None, 'old-buddy'])
def test_pre_upgrade_batch_receipt_replays_original_project_input(project_service, task_store, project):
    old_item = {'description': 'Historical batch', 'tags': [{'name': 'personal', 'is_namespace': True}]}
    if project:
        old_item['tags'].append({'name': f'projects/{project}', 'is_namespace': False})
    original = project_service.batch_create([old_item], client_mutation_id='old-batch', actor='dashboard:user')
    old_request = project_service._prepare_batch_item(old_item, 0, 'old-batch')['request']
    old_request = {key: value for key, value in old_request.items() if key not in {'project', 'project_ids', 'unresolved_projects'}}
    legacy_project_receipt(task_store, 'old-batch', request={'items': [old_request]})
    requested = {'description': 'Historical batch', 'tags': [{'name': 'personal', 'is_namespace': True}]}
    if project:
        requested['project'] = project
    task_store.project_db_path.unlink()
    replay = project_service.batch_create([requested], client_mutation_id='old-batch', actor='dashboard:user')
    assert replay.replayed
    assert replay.tasks[0].task_id == original.tasks[0].task_id
    assert replay.tasks[0].tags == original.tasks[0].tags
    assert len(task_store.list()) == len(task_store.pending_outbox()) == 1
    with pytest.raises(TaskIdempotencyConflict):
        project_service.batch_create([{**requested, 'project_ids': [2]}], client_mutation_id='old-batch', actor='dashboard:user')


def test_modern_tagged_batch_cannot_replay_as_a_new_project_link(project_service):
    project_service.batch_create([{'description': 'Tagged', 'tags': ['projects/old-buddy']}],
                                 client_mutation_id='modern-batch', actor='dashboard:user')
    with pytest.raises(TaskIdempotencyConflict):
        project_service.batch_create([{'description': 'Tagged', 'project': 'old-buddy'}],
                                     client_mutation_id='modern-batch', actor='dashboard:user')


@pytest.mark.parametrize('old_receipt', [False, True])
def test_single_create_old_project_tag_receipt_is_guarded_by_receipt_vintage(project_service, task_store, old_receipt):
    original = create_task(project_service, tags=[Tag('projects/old-buddy', True), Tag('personal', True)])
    if old_receipt:
        legacy_project_receipt(task_store, 'create-1')
        task_store.project_db_path.unlink()
        replay = create_task(project_service, project='old-buddy', tags=[Tag('personal', True)])
        assert replay.replayed
        assert replay.task.task_id == original.task.task_id
        assert len(task_store.history(original.task.task_id)) == len(task_store.pending_outbox()) == 1
    else:
        with pytest.raises(TaskIdempotencyConflict):
            create_task(project_service, project='old-buddy', tags=[Tag('personal', True)])


def test_single_update_without_project_edits_preserves_pre_upgrade_fingerprint(project_service, task_store):
    original = create_task(project_service).task
    updated = project_service.update(original.task_id, changes={'description': 'Edited'}, **authority(original, 'old-update'))
    legacy_project_receipt(task_store, 'old-update', request={
        'task_id': original.task_id, 'expected_revision': original.revision,
        'changes': {'description': 'Edited'}, 'state': None, 'tags': None, 'reason': None,
    })
    replay = project_service.update(original.task_id, changes={'description': 'Edited'}, **authority(original, 'old-update'))
    assert replay.replayed
    assert replay.task.revision == updated.task.revision
    assert len(task_store.history(original.task_id)) == len(task_store.pending_outbox()) == 2


def test_assisted_draft_accepts_bounded_registry_ids_and_rejects_invalid_arrays():
    from work_buddy.dashboard.assistance.contracts import AssistanceError, form_schema, structured_reply_schema, validate_operations, validate_snapshot
    form = form_schema('task-create')
    assert validate_snapshot(form, {'title': 'Shared', 'project_ids': [1, 2]})['project_ids'] == [1, 2]
    assert validate_operations(form, [{'op': 'set', 'path': ['project_ids'], 'value': [1, 2]}])
    for value in ([True], ['work-buddy'], [0], [1.5], '1', [1] * 101):
        with pytest.raises(AssistanceError):
            validate_operations(form, [{'op': 'set', 'path': ['project_ids'], 'value': value}])
    schema = structured_reply_schema(form)
    assert '"items": {"type": "integer", "minimum": 1}' in __import__('json').dumps(schema)
