import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from orca_proxy.desktop import DesktopBackend, JsonListModel


def test_json_list_model_exposes_rows_as_qml_item_maps():
    model = JsonListModel()
    model.replace([{"name": "alpha", "status": "Running"}, {"name": "beta", "status": "Stopped"}])

    assert model.rowCount() == 2
    assert model.count == 2
    assert model.items[0]["name"] == "alpha"
    item_role = int(Qt.ItemDataRole.UserRole) + 1
    assert bytes(model.roleNames()[item_role]) == b"item"
    assert model.data(model.index(1, 0), item_role) == {"name": "beta", "status": "Stopped"}


def test_json_list_model_reset_notifies_qml_and_clears_rows():
    model = JsonListModel()
    changes = []
    model.rowsChanged.connect(lambda: changes.append(model.count))

    model.replace([{"name": "alpha"}])
    model.replace([])

    assert changes == [1, 0]
    assert model.rowCount() == 0


def test_new_vm_inventory_action_opens_the_vm_editor():
    backend = DesktopBackend(demo=True)
    backend.openEditor("vms", "")

    assert backend.editor["kind"] == "vm"


def test_open_files_previews_the_vm_sftp_location_in_demo_mode():
    backend = DesktopBackend(demo=True)
    backend.openFiles("agent-harness")

    assert backend.message == "Preview only: would open sftp://agent-harness/home/ubuntu."


def test_vm_sidebar_sorts_running_first_and_includes_registered_vms():
    backend = DesktopBackend(demo=True)
    backend._registered_vms = [{'name': 'missing', 'ip_address': '10.0.0.1'}]
    backend._replace_raw_vms([{'name': 'alpha', 'status': 'Stopped'}, {'name': 'zebra', 'status': 'Running'}, {'name': 'beta', 'status': 'Running'}])
    assert [r['name'] for r in backend.vmsModel.items] == ['beta', 'zebra', 'alpha', 'missing']


def test_vm_rule_editor_filters_and_addresses_the_selected_scope():
    backend = DesktopBackend(demo=True)
    backend.selectItem('vms', 'agent-harness')
    assert [r['name'] for r in backend.rulesModel.items] == ['github']
    assert [r['name'] for r in backend.vmRulesModel.items] == ['claude']
    backend.openEditor('rules', '')
    assert backend.editor['scope'] == 'agent-harness'
    assert backend.editor['priority'] == 30
    requests = []
    backend._request = lambda method, path, body=None, callback=None: requests.append((method, path, body))
    backend.saveRule({'name': 'shared', 'priority': 10, 'hostname': 'example.com', 'action': {'type': 'allow'}})
    assert requests[-1][1] == '/api/v1/vms/agent-harness/rules/shared'
    assert requests[-1][2]['vm_selector'] == {'type': 'only', 'vms': ['agent-harness']}
    backend.setView('rules')
    backend.openEditor('rules', '')
    assert backend.editor['scope'] == ''
    assert backend.editor['priority'] == 20
    backend.saveRule({'name': 'shared', 'priority': 10, 'hostname': 'example.com', 'action': {'type': 'allow'}})
    assert requests[-1][1] == '/api/v1/rules/shared?scope=global'


def test_deleting_a_vm_rule_keeps_the_vm_selected():
    backend = DesktopBackend(demo=True)
    backend.selectItem('vms', 'agent-harness')
    requests = []
    def request(method, path, body=None, callback=None):
        requests.append(path)
        if callback:
            callback(True, {})
    backend._request = request
    backend.deleteItem('rules', 'claude')
    assert requests == ['/api/v1/vms/agent-harness/rules/claude']
    assert backend.selectionKey == 'agent-harness'
    assert backend.selection['name'] == 'agent-harness'
