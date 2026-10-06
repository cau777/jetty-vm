"""Native desktop UI state and asynchronous loopback API client."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    Property,
    QRunnable,
    QSettings,
    QThreadPool,
    QTimer,
    Qt,
    Signal,
    Slot,
)

from . import config

AGENTS = [
    {"key": "claude", "label": "Claude Code"},
    {"key": "codex", "label": "Codex"},
    {"key": "pi", "label": "Pi"},
    {"key": "opencode", "label": "OpenCode"},
]


class JsonListModel(QAbstractListModel):
    """Small QAbstractListModel that exposes each API row as an `item` map."""

    rowsChanged = Signal()
    _item_role = int(Qt.ItemDataRole.UserRole) + 1

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802 (Qt API)
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        if role in (self._item_role, int(Qt.ItemDataRole.DisplayRole)):
            return self._rows[index.row()]
        return None

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802 (Qt API)
        return {self._item_role: QByteArray(b"item")}

    @Property(int, notify=rowsChanged)
    def count(self) -> int:
        return len(self._rows)

    @Property("QVariantList", notify=rowsChanged)
    def items(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def replace(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()
        self.rowsChanged.emit()


class _RequestSignals(QObject):
    completed = Signal(str, bool, object)


class _HttpRequest(QRunnable):
    def __init__(self, request_id: str, base_url: str, method: str, path: str, body: dict | None):
        super().__init__()
        self.request_id = request_id
        self.base_url = base_url
        self.method = method
        self.path = path
        self.body = body
        self.signals = _RequestSignals()

    def run(self) -> None:
        data = json.dumps(self.body).encode("utf-8") if self.body is not None else None
        request = urllib.request.Request(
            self.base_url + self.path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
            method=self.method,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = response.read()
            parsed = json.loads(payload) if payload else {}
            self.signals.completed.emit(self.request_id, True, parsed)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read())
                error = payload.get("error", payload)
            except (json.JSONDecodeError, AttributeError):
                error = {"message": str(exc)}
            self.signals.completed.emit(self.request_id, False, error)
        except Exception as exc:
            self.signals.completed.emit(self.request_id, False, {"message": str(exc)})


class DesktopBackend(QObject):
    """UI state; every HTTP request executes in QThreadPool workers."""

    activeViewChanged = Signal()
    statusChanged = Signal()
    selectionChanged = Signal()
    editorChanged = Signal()
    jobChanged = Signal()
    messageChanged = Signal()
    projectDirChanged = Signal()

    def __init__(self, *, force_setup: bool = False, demo: bool = False):
        super().__init__()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(8)
        self._workers: dict[str, _HttpRequest] = {}
        self._callbacks: dict[str, Callable[[bool, Any], None]] = {}
        self._base_url = f"http://127.0.0.1:{config.management_api_port()}"
        self._demo = demo
        self._demo_extra_vms: list[dict[str, Any]] = []
        self._demo_step_index = 0
        self._active_view = "vms"
        self._models = {
            "logs": JsonListModel(self),
            "rules": JsonListModel(self),
            "credentials": JsonListModel(self),
            "vms": JsonListModel(self),
        }
        self._registered_vms: list[dict] = []
        self._raw_vms: list[dict] = []
        self._rules: list[dict] = []
        self._system_status: dict[str, Any] = {"state": "unknown", "lxd_available": False}
        self._jetty_status: dict[str, Any] = {"gateway": "unknown", "vms": []}
        self._ready = False
        self._force_setup = force_setup
        self._selected_key = ""
        self._selection: dict[str, Any] = {}
        self._log_details: dict[str, Any] = {}
        self._editor: dict[str, Any] = {}
        self._error_message = ""
        self._error_fields: dict[str, str] = {}
        self._job: dict[str, Any] = {"state": "idle", "steps": []}
        self._job_polling = False
        self._message = ""
        self._message_tone = "info"
        self._handoff_message = ""
        self._settings = QSettings("Jetty", "Jetty")
        self._project_dir = str(self._settings.value("projectDir", str(Path.home())))
        self._quick_adds = self._load_catalog()
        self._refresh_timer = QTimer(self, interval=5000, timeout=self.refresh)
        self._refresh_timer.start()
        self._job_timer = QTimer(self, interval=800, timeout=self._poll_job)
        self._demo_job_timer = QTimer(self, interval=350, timeout=self._advance_demo_job)
        self.refresh()

    @staticmethod
    def _load_catalog() -> list[dict[str, Any]]:
        catalog = Path(__file__).parent / "resources" / "quick-add-catalog.json"
        try:
            return json.loads(catalog.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        callback: Callable[[bool, Any], None] | None = None,
    ) -> str:
        request_id = uuid.uuid4().hex
        worker = _HttpRequest(request_id, self._base_url, method, path, body)
        worker.signals.completed.connect(self._request_finished)
        self._workers[request_id] = worker
        if callback is not None:
            self._callbacks[request_id] = callback
        self._pool.start(worker)
        return request_id

    @Slot(str, bool, object)
    def _request_finished(self, request_id: str, ok: bool, payload: Any) -> None:
        self._workers.pop(request_id, None)
        callback = self._callbacks.pop(request_id, None)
        if callback is not None:
            callback(ok, payload)

    def _notify(self, message: str, tone: str = "info") -> None:
        self._message = message
        self._message_tone = tone
        self.messageChanged.emit()

    def _load(self, path: str, callback: Callable[[dict], None]) -> None:
        self._request("GET", path, callback=lambda ok, body: callback(body) if ok else None)

    @Slot()
    def refresh(self) -> None:
        if self._demo:
            self._load_demo()
            return

        def update_status(body):
            self._system_status = body
            self.statusChanged.emit()

        def update_jetty(body):
            self._jetty_status = body
            self._raw_vms = body.get("vms", [])
            self._rebuild_vms()
            self.statusChanged.emit()

        def update_registered(body):
            self._registered_vms = body.get("vms", [])
            self._rebuild_vms()

        def update_rules(body):
            self._rules = body.get("rules", [])
            self.rulesModel.replace(sorted(self._rules, key=lambda row: row.get("priority", 0)))
            self._rebuild_vms()

        self._load("/api/v1/status", update_status)
        self._load("/api/v1/jetty/status", update_jetty)
        self._load("/api/v1/jetty/vms", lambda body: self._replace_raw_vms(body.get("vms", [])))
        self._load("/api/v1/vms", update_registered)
        self._load("/api/v1/rules", update_rules)
        self._load("/api/v1/credentials", lambda body: self.credentialsModel.replace(body.get("credentials", [])))
        self._load("/api/v1/requests?limit=100", lambda body: self.connectionsModel.replace(body.get("connections", [])))
        self._load("/readyz", lambda body: self._set_ready(bool(body.get("ready"))))

    def _replace_raw_vms(self, vms: list[dict]) -> None:
        self._raw_vms = vms
        self._rebuild_vms()

    def _rebuild_vms(self) -> None:
        registered = {row["name"]: row for row in self._registered_vms}
        rows = []
        for vm in self._raw_vms:
            rules = sum(
                1
                for rule in self._rules
                if rule.get("vm_selector", {}).get("type") == "all"
                or vm.get("name") in rule.get("vm_selector", {}).get("vms", [])
            )
            rows.append({**registered.get(vm.get("name"), {}), **vm, "rules": rules})
        self.vmsModel.replace(sorted(rows, key=lambda row: row.get("name", "")))
        self._refresh_selection()

    def _set_ready(self, ready: bool) -> None:
        self._ready = ready
        self.statusChanged.emit()

    def _load_demo(self) -> None:
        self._system_status = {"state": "running", "proxy": "running", "lxd_available": True}
        self._jetty_status = {"gateway": "running", "gateway_service": "active", "tunnel": "connected"}
        self._ready = True
        self._raw_vms = [
            *self._demo_extra_vms,
            {"name": "agent-harness", "ip_address": "10.202.0.11", "status": "Running"},
            {"name": "sales-leads", "ip_address": "10.202.0.14", "status": "Stopped"},
        ]
        self._registered_vms = [{"name": "agent-harness", "created_at": "2026-10-01T18:30:00Z"}]
        self._rules = [
            {"name": "github", "priority": 10, "hostname": "api.github.com", "vm_selector": {"type": "all"}, "action": {"type": "allow"}},
            {"name": "claude", "priority": 20, "hostname": "api.anthropic.com", "vm_selector": {"type": "only", "vms": ["agent-harness"]}, "action": {"type": "allow_with_credential", "credential": "claude-code-subscription", "path_prefix": "/", "injection": {"type": "bearer"}}},
        ]
        self.rulesModel.replace(self._rules)
        self.credentialsModel.replace([{"name": "claude-code-subscription", "command": "claude-token", "ttl_seconds": 1800, "status": "valid", "expires_at": None}])
        self.connectionsModel.replace([{"id": 42, "started_at": "2026-10-05T17:30:00Z", "vm_name": "agent-harness", "destination_hostname": "api.github.com", "destination_ip": "140.82.112.5", "destination_port": 443, "sni_present": True, "ech_present": False, "intercepted": False, "outcome": "allow_rule", "duration_ms": 81, "matched_rule": {"name": "github", "priority": 10}}])
        self._rebuild_vms()
        self.statusChanged.emit()

    @Property(QObject, constant=True)
    def vmsModel(self):
        return self._models["vms"]

    @Property(QObject, constant=True)
    def rulesModel(self):
        return self._models["rules"]

    @Property(QObject, constant=True)
    def credentialsModel(self):
        return self._models["credentials"]

    @Property(QObject, constant=True)
    def connectionsModel(self):
        return self._models["logs"]

    @Property("QVariantList", constant=True)
    def agents(self):
        return AGENTS

    @Property("QVariantList", constant=True)
    def quickAdds(self):
        return self._quick_adds

    @Property(str, constant=True)
    def homeDir(self) -> str:
        return str(Path.home())

    @Property(str, notify=projectDirChanged)
    def projectDir(self) -> str:
        return self._project_dir

    @Property(str, notify=activeViewChanged)
    def activeView(self) -> str:
        return self._active_view

    @Property(QObject, notify=activeViewChanged)
    def currentModel(self):
        return self._models["logs" if self._active_view == "logs" else self._active_view]

    @Property(str, notify=activeViewChanged)
    def viewTitle(self) -> str:
        return {"logs": "Connection log", "rules": "Rules", "credentials": "Credentials", "vms": "Virtual machines"}.get(self._active_view, "Jetty")

    @Property(str, notify=activeViewChanged)
    def viewSubtitle(self) -> str:
        return {
            "logs": "Inspect outbound requests and the rules that handled them.",
            "rules": "Control which destinations each VM can reach.",
            "credentials": "Manage host-side credential providers without exposing their values.",
            "vms": "Create and manage isolated LXD agent machines.",
        }.get(self._active_view, "")

    @Property(str, notify=statusChanged)
    def systemState(self) -> str:
        return str(self._system_status.get("state", "unknown"))

    @Property(str, notify=statusChanged)
    def proxyState(self) -> str:
        return str(self._system_status.get("proxy", "unknown"))

    @Property(bool, notify=statusChanged)
    def lxdAvailable(self) -> bool:
        return bool(self._system_status.get("lxd_available"))

    @Property(str, notify=statusChanged)
    def gatewayState(self) -> str:
        return str(self._jetty_status.get("gateway", "unknown"))

    @Property(str, notify=statusChanged)
    def gatewayService(self) -> str:
        return str(self._jetty_status.get("gateway_service", "unknown"))

    @Property(str, notify=statusChanged)
    def tunnelState(self) -> str:
        return str(self._jetty_status.get("tunnel", "unknown"))

    @Property(bool, notify=statusChanged)
    def ready(self) -> bool:
        return self._ready

    @Property(bool, notify=statusChanged)
    def needsSetup(self) -> bool:
        return self._force_setup or self.systemState in {"not_configured", "waiting_for_network"} or self.gatewayState in {"missing", "stopped"}

    @Property(str, notify=statusChanged)
    def statusMessage(self) -> str:
        return str(self._system_status.get("proxy_error") or self._jetty_status.get("message") or self._system_status.get("message") or "")

    @Property(str, notify=selectionChanged)
    def selectionKey(self) -> str:
        return self._selected_key

    @Property("QVariantMap", notify=selectionChanged)
    def selection(self) -> dict:
        return dict(self._selection)

    @Property("QVariantMap", notify=selectionChanged)
    def logDetails(self) -> dict:
        return dict(self._log_details)

    @Property("QVariantMap", notify=editorChanged)
    def editor(self) -> dict:
        return dict(self._editor)

    @Property("QVariantMap", notify=editorChanged)
    def errorFields(self) -> dict:
        return dict(self._error_fields)

    @Property(str, notify=editorChanged)
    def errorMessage(self) -> str:
        return self._error_message

    @Property("QVariantMap", notify=jobChanged)
    def job(self) -> dict:
        return dict(self._job)

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property(str, notify=messageChanged)
    def messageTone(self) -> str:
        return self._message_tone

    @Property(str, notify=messageChanged)
    def handoffMessage(self) -> str:
        return self._handoff_message

    @Slot(str)
    def setView(self, view: str) -> None:
        if view not in self._models or view == self._active_view:
            return
        self._active_view = view
        self._selected_key = ""
        self._selection = {}
        self._log_details = {}
        self.activeViewChanged.emit()
        self.selectionChanged.emit()
        self.refresh()

    @Slot(str, str)
    def selectItem(self, view: str, key: str) -> None:
        if view not in self._models:
            return
        self._active_view = view
        self._selected_key = key
        row = next((item for item in self._models[view].items if self._key_for(view, item) == key), {})
        self._selection = dict(row)
        self._log_details = {}
        self.activeViewChanged.emit()
        self.selectionChanged.emit()
        if view == "logs" and key:
            if self._demo:
                self._set_log_details({
                    **row,
                    "matched_rule": {"name": "github", "priority": 10, "action": {"type": "allow"}},
                    "http_requests": [{
                        "method": "GET", "path": "/user", "outcome": "allow_rule", "status": 200,
                        "status_origin": "upstream", "latency_ms": 81, "matched_credential": None,
                        "matched_rule": {"name": "github", "priority": 10},
                        "trace": [{"rule": "github", "result": "matched", "reason": "host and VM selector matched"}],
                        "headers": [{"name": "authorization", "value": "[redacted]"}, {"name": "accept", "value": "application/json"}],
                    }],
                })
            else:
                self._load(f"/api/v1/requests/{urllib.parse.quote(key, safe='')}", self._set_log_details)

    @staticmethod
    def _key_for(view: str, item: dict) -> str:
        return str(item.get("id", "")) if view == "logs" else str(item.get("name", ""))

    def _set_log_details(self, body: dict) -> None:
        if self._active_view == "logs" and str(body.get("id", "")) == self._selected_key:
            self._log_details = body
            self.selectionChanged.emit()

    def _refresh_selection(self) -> None:
        if not self._selected_key or self._active_view not in self._models:
            return
        row = next((item for item in self._models[self._active_view].items if self._key_for(self._active_view, item) == self._selected_key), None)
        if row is None:
            self._selection = {}
            self._selected_key = ""
        else:
            self._selection = dict(row)
        self.selectionChanged.emit()

    @Slot(str, str)
    def openEditor(self, kind: str, name: str = "") -> None:
        if kind == "vms":
            kind = "vm"
        self._error_fields = {}
        self._error_message = ""
        model = self._models.get(kind)
        row = next((item for item in model.items if item.get("name") == name), {}) if name and model else {}
        self._editor = {"kind": kind, "mode": "edit" if name else "create", **row}
        if kind == "vm":
            self._editor.update({"cpus": 4, "memory": "8GiB", "disk": "40GiB", "image": "ubuntu:24.04"})
        elif kind == "rules" and not name:
            priorities = [int(rule.get("priority", 0)) for rule in self._rules]
            self._editor["priority"] = max(priorities, default=0) + 10
        self.editorChanged.emit()

    @Slot()
    def closeEditor(self) -> None:
        self._editor = {}
        self._error_message = ""
        self._error_fields = {}
        self.editorChanged.emit()

    @Slot("QVariantMap")
    def saveRule(self, form: dict) -> None:
        name = str(form.get("name", "")).strip()
        body = {key: form.get(key) for key in ("priority", "vm_selector", "hostname", "action")}
        self._save_resource("rules", name, body)

    @Slot("QVariantMap")
    def saveCredential(self, form: dict) -> None:
        name = str(form.get("name", "")).strip()
        try:
            ttl = int(form.get("ttl_seconds", 0))
        except (ValueError, TypeError):
            ttl = -1
        self._save_resource("credentials", name, {"command": form.get("command", ""), "ttl_seconds": ttl})

    def _save_resource(self, kind: str, name: str, body: dict) -> None:
        self._error_fields = {}
        self._error_message = ""
        self.editorChanged.emit()
        encoded = urllib.parse.quote(name, safe="")

        def complete(ok, result):
            if not ok:
                self._set_form_error(result)
                return
            self._notify(f"{kind[:-1].title()} saved.", "success")
            self.closeEditor()
            self.refresh()

        self._request("PUT", f"/api/v1/{kind}/{encoded}", body, complete)

    def _set_form_error(self, error: Any) -> None:
        error = error if isinstance(error, dict) else {"message": str(error)}
        self._error_message = str(error.get("message", "The request failed."))
        self._error_fields = error.get("fields", {}) if isinstance(error.get("fields"), dict) else {}
        self.editorChanged.emit()
        self._notify(self._error_message, "danger")

    @Slot(str, str)
    def deleteItem(self, kind: str, name: str) -> None:
        encoded = urllib.parse.quote(name, safe="")

        def complete(ok, error):
            if not ok:
                self._notify(str(error.get("message", "Delete failed.")), "danger")
                return
            self._notify(f"{kind[:-1].title()} deleted.", "success")
            self._selection = {}
            self._selected_key = ""
            self.selectionChanged.emit()
            self.refresh()

        self._request("DELETE", f"/api/v1/{kind}/{encoded}", callback=complete)

    @Slot(str)
    def deleteVm(self, name: str) -> None:
        encoded = urllib.parse.quote(name, safe="")

        def complete(ok, result):
            if not ok:
                self._notify(str(result.get("message", "VM delete failed.")), "danger")
                return
            self._notify(f"{name} deleted.", "success")
            self._selection = {}
            self._selected_key = ""
            self.selectionChanged.emit()
            self.refresh()

        self._request("DELETE", f"/api/v1/jetty/vms/{encoded}", callback=complete)

    @Slot(str, str)
    def vmAction(self, name: str, action: str) -> None:
        encoded = urllib.parse.quote(name, safe="")

        def complete(ok, result):
            if not ok:
                self._notify(str(result.get("message", "VM action failed.")), "danger")
                return
            self._notify(f"{name}: {result.get('status', action)}", "success")
            self.refresh()

        self._request("POST", f"/api/v1/jetty/vms/{encoded}/action", {"action": action}, complete)

    @Slot("QVariantMap")
    def startCreate(self, form: dict) -> None:
        if self._demo:
            self._start_demo_job("vm_create", form)
            return
        payload = {key: form.get(key) for key in ("name", "cpus", "memory", "disk", "image")}
        if form.get("ssh_public_key"):
            payload["ssh_public_key"] = form["ssh_public_key"]
        self._start_job("/api/v1/jetty/vms", payload)

    @Slot(str)
    def startSetup(self, ssh_key: str = "") -> None:
        self._force_setup = True
        if self._demo:
            self._start_demo_job("gateway_setup", {"name": "jetty-gw"})
            return
        body = {"ssh_public_key": ssh_key.strip()} if ssh_key.strip() else {}
        self._start_job("/api/v1/jetty/setup", body)

    def _start_job(self, path: str, body: dict) -> None:
        self._job = {"state": "submitting", "steps": []}
        self._error_fields = {}
        self._error_message = ""
        self.jobChanged.emit()

        def accepted(ok, result):
            if not ok:
                self._job = {"state": "failed", "error": result, "steps": []}
                self._error_fields = result.get("fields", {}) if isinstance(result, dict) else {}
                self._error_message = str(result.get("message", "Operation could not start.")) if isinstance(result, dict) else str(result)
                self.jobChanged.emit()
                self.editorChanged.emit()
                self._notify(self._error_message, "danger")
                self.refresh()
                return
            self._job = result
            self.jobChanged.emit()
            self._job_timer.start()
            self._poll_job()

        self._request("POST", path, body, accepted)

    @Slot()
    def _poll_job(self) -> None:
        job_id = self._job.get("job_id")
        if not job_id or self._job_polling:
            return
        self._job_polling = True

        def updated(ok, result):
            self._job_polling = False
            if not ok:
                self._job_timer.stop()
                self._job = {**self._job, "state": "failed", "error": result}
                self.jobChanged.emit()
                return
            self._job = result
            self.jobChanged.emit()
            if result.get("state") in {"done", "failed", "cancelled"}:
                self._job_timer.stop()
                if result.get("state") == "done":
                    self._force_setup = False
                    self._notify("Operation completed successfully.", "success")
                else:
                    error = result.get("error") or {}
                    self._notify(str(error.get("message", "Operation failed.")), "danger")
                self.refresh()
                self.statusChanged.emit()

        self._request("GET", f"/api/v1/jetty/jobs/{urllib.parse.quote(job_id, safe='')}", callback=updated)

    @Slot()
    def dismissJob(self) -> None:
        self._job_timer.stop()
        self._demo_job_timer.stop()
        self._job = {"state": "idle", "steps": []}
        self.jobChanged.emit()

    @Slot(str, str, str)
    def handoff(self, vm_name: str, agent: str, project_dir: str) -> None:
        if self._demo:
            self._handoff_message = f"Preview only: would open {agent} for {vm_name} in {project_dir}."
            self.messageChanged.emit()
            return
        self._handoff_message = f"Opening {agent} for {vm_name}…"
        self.messageChanged.emit()
        path = f"/api/v1/jetty/vms/{urllib.parse.quote(vm_name, safe='')}/handoff"

        def complete(ok, result):
            self._handoff_message = result.get("message", f"{agent} terminal opened for {vm_name}.") if ok else result.get("message", "Could not open the agent terminal.")
            self.messageChanged.emit()

        self._request("POST", path, {"agent": agent, "project_dir": project_dir}, complete)

    @Slot()
    def showSetup(self) -> None:
        self._force_setup = True
        self.statusChanged.emit()

    @Slot()
    def hideSetup(self) -> None:
        self._force_setup = False
        self.statusChanged.emit()

    @Slot()
    def clearMessage(self) -> None:
        self._message = ""
        self.messageChanged.emit()

    @Slot(str)
    def setProjectDir(self, value: str) -> None:
        self._project_dir = value.strip() or str(Path.home())
        self._settings.setValue("projectDir", self._project_dir)
        self.projectDirChanged.emit()

    @Slot(int, result="QVariantMap")
    def quickAdd(self, index: int) -> dict:
        return dict(self._quick_adds[index]) if 0 <= index < len(self._quick_adds) else {}

    def _start_demo_job(self, kind: str, form: dict) -> None:
        if kind == "vm_create":
            steps = [
                ("validate", "Validating VM settings"),
                ("instance", "Pulling the Ubuntu image and creating the instance"),
                ("register", "Registering the VM and SSH access"),
                ("start", "Starting the VM"),
                ("guest_agent", "Waiting for the LXD guest agent"),
                ("cloud_init", "Running cloud-init"),
                ("ready", "Checking the VM is ready"),
            ]
        else:
            steps = [
                ("validate", "Checking the SSH key"),
                ("project", "Preparing the LXD project"),
                ("profile", "Preparing the VM profile"),
                ("networks", "Preparing the Jetty networks"),
                ("instance", "Creating the gateway if needed"),
                ("start", "Starting the gateway"),
                ("guest_agent", "Waiting for the LXD guest agent"),
                ("cloud_init", "Running cloud-init"),
                ("configure", "Installing gateway networking and firewall rules"),
                ("ssh_config", "Updating host SSH configuration"),
            ]
        self._job_timer.stop()
        self._demo_step_index = 0
        self._job = {
            "kind": kind,
            "name": form.get("name", "demo-vm"),
            "state": "running",
            "steps": [{"key": key, "label": label, "state": "pending"} for key, label in steps],
            "current_step": steps[0][0],
            "elapsed_seconds": 0,
            "result": None,
            "error": None,
        }
        self._job["steps"][0]["state"] = "running"
        self._demo_job_timer.start()
        self.jobChanged.emit()

    @Slot()
    def _advance_demo_job(self) -> None:
        self._job["elapsed_seconds"] = round(float(self._job.get("elapsed_seconds", 0)) + 0.35, 2)
        steps = self._job["steps"]
        index = self._demo_step_index
        should_fail = self._job.get("kind") == "vm_create" and "fail" in str(self._job.get("name", ""))
        if should_fail and index == 5:
            steps[index]["state"] = "failed"
            self._job["state"] = "failed"
            self._job["error"] = {"message": "cloud-init status --wait exited 2. The VM exists, but setup did not finish."}
            self._demo_job_timer.stop()
        elif index + 1 >= len(steps):
            for step in steps:
                step["state"] = "done"
            self._job["state"] = "done"
            if self._job["kind"] == "vm_create":
                name = str(self._job.get("name", "demo-vm"))
                result = {"name": name, "ip_address": f"10.202.0.{20 + len(self._demo_extra_vms)}", "status": "Running"}
                self._job["result"] = result
                self._demo_extra_vms.insert(0, {**result, "rules": 0, "fresh": True})
                self._rebuild_vms()
            else:
                self._job["result"] = {"gateway": "jetty-gw", "state": "ready"}
            self._demo_job_timer.stop()
        else:
            steps[index]["state"] = "done"
            self._demo_step_index += 1
            next_step = self._demo_step_index
            steps[next_step]["state"] = "running"
            self._job["current_step"] = steps[next_step]["key"]
        self.jobChanged.emit()
