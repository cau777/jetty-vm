import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

Pane {
    id: root
    required property var backend
    signal confirmationRequested(string kind, string name, string action)
    padding: 18
    Material.background: "#10151d"

    ScrollView {
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth
        ColumnLayout {
            width: parent.width
            spacing: 12

            Label { text: "SELECTION INSPECTOR"; color: "#72e0bd"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.1 }
            Label {
                Layout.fillWidth: true
                visible: !root.backend.selection.name && root.backend.activeView !== "logs"
                text: "Select a row to inspect it."
                color: "#8793a5"
                wrapMode: Text.Wrap
            }
            Label {
                Layout.fillWidth: true
                visible: root.backend.activeView === "logs" && !root.backend.selection.id
                text: "Select a connection to inspect its rule evaluation and HTTP requests."
                color: "#8793a5"
                wrapMode: Text.Wrap
            }

            // VM details and actions
            ColumnLayout {
                Layout.fillWidth: true
                visible: root.backend.activeView === "vms" && !!root.backend.selection.name
                spacing: 12
                Label { text: root.backend.selection.name || ""; font.pixelSize: 19; font.bold: true; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                GridLayout {
                    columns: 2
                    columnSpacing: 12
                    rowSpacing: 7
                    Label { text: "Status"; color: "#8793a5" }
                    Label { text: root.backend.selection.status || "unknown"; color: root.backend.selection.status === "Running" ? "#72e0bd" : "#ffc56e" }
                    Label { text: "IP address"; color: "#8793a5" }
                    Label { text: root.backend.selection.ip_address || "—"; font.family: "monospace" }
                    Label { text: "Rules"; color: "#8793a5" }
                    Label { text: root.backend.selection.rules === undefined ? "—" : root.backend.selection.rules }
                    Label { text: "Registered"; color: "#8793a5" }
                    Label { text: root.backend.selection.created_at || "Not registered"; font.pixelSize: 11; wrapMode: Text.WrapAnywhere }
                }
                Flow {
                    Layout.fillWidth: true
                    spacing: 6
                    JettyButton {
                        text: root.backend.selection.status === "Running" ? "Stop" : "Start"
                        onClicked: root.confirmationRequested("vm", root.backend.selection.name, root.backend.selection.status === "Running" ? "stop" : "start")
                    }
                    JettyButton { text: "Restart"; flat: true; onClicked: root.confirmationRequested("vm", root.backend.selection.name, "restart") }
                    JettyButton { text: "Delete"; destructive: true; onClicked: root.confirmationRequested("vm", root.backend.selection.name, "delete") }
                }
                Rectangle { Layout.fillWidth: true; height: 1; color: "#2b3544" }
                Label { text: "PREPARE WITH AN AGENT"; color: "#72e0bd"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.1 }
                AgentHandoff { Layout.fillWidth: true; backend: root.backend; vmName: root.backend.selection.name || "" }
            }

            // Rule details
            ColumnLayout {
                Layout.fillWidth: true
                visible: root.backend.activeView === "rules" && !!root.backend.selection.name
                spacing: 9
                Label { text: root.backend.selection.name || ""; font.pixelSize: 19; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                Label { text: "Priority " + (root.backend.selection.priority === undefined ? "—" : root.backend.selection.priority) + " · " + (root.backend.selection.hostname || ""); color: "#aab5c4"; wrapMode: Text.WrapAnywhere; Layout.fillWidth: true }
                Label {
                    Layout.fillWidth: true
                    text: "VMs: " + (root.backend.selection.vm_selector && root.backend.selection.vm_selector.type === "all" ? "all" : (root.backend.selection.vm_selector && root.backend.selection.vm_selector.vms || []).join(", "))
                    color: "#8793a5"
                    wrapMode: Text.Wrap
                }
                Label { Layout.fillWidth: true; text: JSON.stringify(root.backend.selection.action || {}); color: "#7eb7ff"; wrapMode: Text.WrapAnywhere; font.family: "monospace"; font.pixelSize: 11 }
                RowLayout {
                    JettyButton { text: "Edit"; flat: true; onClicked: root.backend.openEditor("rules", root.backend.selection.name) }
                    JettyButton { text: "Delete"; destructive: true; onClicked: root.confirmationRequested("rules", root.backend.selection.name, "delete") }
                }
            }

            // Credential details
            ColumnLayout {
                Layout.fillWidth: true
                visible: root.backend.activeView === "credentials" && !!root.backend.selection.name
                spacing: 9
                Label { text: root.backend.selection.name || ""; font.pixelSize: 19; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                Label { text: "Live status: " + (root.backend.selection.status || "unknown"); color: root.backend.selection.status === "valid" ? "#72e0bd" : root.backend.selection.status === "error" ? "#ff7b7b" : "#ffc56e" }
                Label { text: "Cache TTL: " + (root.backend.selection.ttl_seconds === 0 ? "no cache" : (root.backend.selection.ttl_seconds || 0) + " seconds"); color: "#aab5c4" }
                Label { text: "Provider command"; color: "#8793a5"; font.pixelSize: 11 }
                TextArea { Layout.fillWidth: true; readOnly: true; text: root.backend.selection.command || ""; wrapMode: TextEdit.Wrap; selectByMouse: true; font.family: "monospace"; font.pixelSize: 11; implicitHeight: 110 }
                Label { Layout.fillWidth: true; text: root.backend.selection.failure_category ? "Last failure: " + root.backend.selection.failure_category : root.backend.selection.last_success_at ? "Last success: " + root.backend.selection.last_success_at : "No cached credential value"; color: "#8793a5"; wrapMode: Text.Wrap; font.pixelSize: 11 }
                RowLayout {
                    JettyButton { text: "Edit"; flat: true; onClicked: root.backend.openEditor("credentials", root.backend.selection.name) }
                    JettyButton { text: "Clear cache"; flat: true; onClicked: root.backend.refreshCredential(root.backend.selection.name) }
                    JettyButton { text: "Delete"; destructive: true; onClicked: root.confirmationRequested("credentials", root.backend.selection.name, "delete") }
                }
                Label { Layout.fillWidth: true; text: "Credential values and command output are never shown here."; color: "#8793a5"; wrapMode: Text.Wrap; font.pixelSize: 11 }
            }

            // Connection details and each intercepted HTTP request
            ColumnLayout {
                Layout.fillWidth: true
                visible: root.backend.activeView === "logs" && !!root.backend.selection.id
                spacing: 10
                Label { text: root.backend.selection.destination_hostname || root.backend.selection.destination_ip || "Connection"; font.pixelSize: 17; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }
                Label { text: root.backend.selection.vm_name || ""; color: "#7eb7ff" }
                Label { Layout.fillWidth: true; text: (root.backend.selection.destination_ip || "") + ":" + (root.backend.selection.destination_port || ""); color: "#aab5c4"; font.family: "monospace" }
                Label { Layout.fillWidth: true; text: "Started " + (root.backend.selection.started_at || "—") + " · " + (root.backend.selection.duration_ms === null ? "—" : (root.backend.selection.duration_ms + " ms")); color: "#8793a5"; wrapMode: Text.Wrap }
                Label { Layout.fillWidth: true; text: "Decision: " + (root.backend.selection.outcome || "unknown") + (root.backend.selection.intercepted ? " · intercepted" : ""); color: "#aab5c4"; wrapMode: Text.Wrap }
                Label { Layout.fillWidth: true; text: "SNI " + (root.backend.selection.sni_present ? "present" : "absent") + " · ECH " + (root.backend.selection.ech_present ? "present" : "absent"); color: "#8793a5"; wrapMode: Text.Wrap }
                Label { visible: !!root.backend.selection.matched_rule; text: "Matched rule"; color: "#8793a5"; font.pixelSize: 11 }
                Label { Layout.fillWidth: true; visible: !!root.backend.selection.matched_rule; text: JSON.stringify(root.backend.selection.matched_rule || {}); color: "#7eb7ff"; wrapMode: Text.WrapAnywhere; font.family: "monospace"; font.pixelSize: 10 }
                Label { visible: !!root.backend.selection.intercepted_by_rule; text: "Intercepted by"; color: "#8793a5"; font.pixelSize: 11 }
                Label { Layout.fillWidth: true; visible: !!root.backend.selection.intercepted_by_rule; text: JSON.stringify(root.backend.selection.intercepted_by_rule || {}); color: "#7eb7ff"; wrapMode: Text.WrapAnywhere; font.family: "monospace"; font.pixelSize: 10 }
                Rectangle { Layout.fillWidth: true; height: 1; color: "#2b3544" }
                Label { text: "HTTP requests"; font.bold: true }
                Repeater {
                    model: root.backend.logDetails.http_requests || []
                    delegate: Pane {
                        required property var modelData
                        Layout.fillWidth: true
                        padding: 10
                        Material.background: "#0c1017"
                        ColumnLayout {
                            width: parent.width
                            spacing: 5
                            Label { Layout.fillWidth: true; text: modelData.method + " " + modelData.path; font.family: "monospace"; font.bold: true; wrapMode: Text.WrapAnywhere }
                            Label { Layout.fillWidth: true; text: "Outcome " + modelData.outcome + " · status " + (modelData.status === null ? "—" : modelData.status) + " (" + (modelData.status_origin || "unknown") + ") · " + (modelData.latency_ms === null ? "—" : modelData.latency_ms + " ms"); color: "#aab5c4"; wrapMode: Text.Wrap }
                            Label { Layout.fillWidth: true; text: "Credential: " + (modelData.matched_credential || "none") + " · rule: " + JSON.stringify(modelData.matched_rule || {}); color: "#8793a5"; wrapMode: Text.WrapAnywhere; font.pixelSize: 10 }
                            Label { text: "Rule evaluation trace"; color: "#8793a5"; font.pixelSize: 10 }
                            TextArea { Layout.fillWidth: true; readOnly: true; text: JSON.stringify(modelData.trace || [], null, 2); wrapMode: TextEdit.Wrap; selectByMouse: true; font.family: "monospace"; font.pixelSize: 9; implicitHeight: Math.max(90, Math.min(180, 35 + (modelData.trace || []).length * 23)) }
                            Label { text: "Redacted headers"; color: "#8793a5"; font.pixelSize: 10 }
                            TextArea { Layout.fillWidth: true; readOnly: true; text: JSON.stringify(modelData.headers || [], null, 2); wrapMode: TextEdit.Wrap; selectByMouse: true; font.family: "monospace"; font.pixelSize: 9; implicitHeight: Math.max(80, Math.min(150, 35 + (modelData.headers || []).length * 20)) }
                        }
                    }
                }
                Label { Layout.fillWidth: true; visible: root.backend.logDetails.http_requests !== undefined && root.backend.logDetails.http_requests.length === 0; text: "This connection did not contain intercepted HTTP requests."; color: "#8793a5"; wrapMode: Text.Wrap }
            }
        }
    }
}
