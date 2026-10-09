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
