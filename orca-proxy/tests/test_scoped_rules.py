import json
import shutil

from orca_proxy import db
from orca_proxy.repo import rules as rules_repo


async def _vm(client, name, ip):
    response = await client.put(f'/api/v1/vms/{name}', json={'ip_address': ip})
    assert response.status == 201


def _body(priority=10, action='allow'):
    return {'priority': priority, 'hostname': 'api.github.com', 'action': {'type': action}}


async def test_rule_names_and_priorities_are_local_to_the_scope(client):
    await _vm(client, 'alpha', '10.0.0.1')
    await _vm(client, 'beta', '10.0.0.2')
    for path in ['/api/v1/vms/alpha/rules/shared', '/api/v1/vms/beta/rules/shared', '/api/v1/rules/shared?scope=global']:
        response = await client.put(path, json=_body())
        assert response.status == 201
    response = await client.put('/api/v1/vms/alpha/rules/other', json=_body())
    assert response.status == 409
    response = await client.put('/api/v1/vms/alpha/rules/shared', json=_body(action='block'))
    assert response.status == 200
    response = await client.get('/api/v1/vms/beta/rules/shared')
    assert (await response.json())['action']['type'] == 'allow'
    response = await client.get('/api/v1/rules/shared')
    assert response.status == 409  # Never guess which duplicate name to use.
    response = await client.delete('/api/v1/vms/alpha/rules/shared')
    assert response.status == 204
    for path in ['/api/v1/vms/beta/rules/shared', '/api/v1/rules/shared?scope=global']:
        response = await client.get(path)
        assert response.status == 200


async def test_scoped_api_does_not_allow_moving_or_sharing_a_rule(client):
    await _vm(client, 'alpha', '10.0.0.1')
    await _vm(client, 'beta', '10.0.0.2')
    response = await client.put('/api/v1/vms/alpha/rules/test', json={**_body(), 'vm_selector': {'type': 'only', 'vms': ['beta']}})
    assert response.status == 422
    response = await client.put('/api/v1/rules/test', json={**_body(), 'vm_selector': {'type': 'only', 'vms': ['alpha', 'beta']}})
    assert response.status == 422
    response = await client.put('/api/v1/vms/missing/rules/test', json=_body())
    assert response.status == 404


async def test_rule_lists_filter_by_scope(client):
    await _vm(client, 'alpha', '10.0.0.1')
    for path in ['/api/v1/vms/alpha/rules/local', '/api/v1/rules/global?scope=global']:
        assert (await client.put(path, json=_body())).status == 201
    response = await client.get('/api/v1/rules?scope=global')
    assert [r['name'] for r in (await response.json())['rules']] == ['global']
    response = await client.get('/api/v1/vms/alpha/rules')
    assert [r['name'] for r in (await response.json())['rules']] == ['local']


def test_migration_preserves_global_rules_and_splits_legacy_vm_groups(tmp_path):
    legacy = tmp_path / 'legacy-migrations'
    legacy.mkdir()
    for source in db.STATE_MIGRATIONS_DIR.glob('*.sql'):
        if int(source.name.split('_')[0]) < 4:
            shutil.copy2(source, legacy / source.name)
    conn = db.connect(tmp_path / 'state.sqlite')
    db.migrate(conn, legacy)
    for name, priority, selector in [
        ('global', 1, {'type': 'all'}),
        ('shared', 10, {'type': 'only', 'vms': ['alpha', 'beta']}),
    ]:
        conn.execute('INSERT INTO rules VALUES (?, ?, ?, ?, ?, ?, ?)', (name, priority, json.dumps(selector), 'example.com', '{"type":"allow"}', 'created', 'updated'))
    db.migrate(conn)
    rows = [rules_repo.to_dict(r) for r in rules_repo.list_all(conn)]
    assert [(r['scope'], r['name'], r['priority']) for r in rows] == [('', 'global', 1), ('alpha', 'shared', 10), ('beta', 'shared', 10)]
    assert all(r['created_at'] == 'created' and r['updated_at'] == 'updated' for r in rows)
    assert rows[1]['vm_selector'] == {'type': 'only', 'vms': ['alpha']}
    db.migrate(conn)  # A repeated startup must not duplicate the copies.
    assert len(rules_repo.list_all(conn)) == 3
    conn.close()
