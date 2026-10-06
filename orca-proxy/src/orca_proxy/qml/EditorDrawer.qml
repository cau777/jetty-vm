import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Dialogs
import QtQuick.Layouts

Drawer {
    id: root
    required property var backend
    edge: Qt.RightEdge
    width: Math.min(560, parent.width)
    height: parent.height
    modal: true
    closePolicy: Popup.CloseOnEscape
    Material.background: "#111720"
    background: Rectangle { color: "#111720"; border.color: "#2b3544"; border.width: 1 }
    Overlay.modal: Rectangle { color: "#b3000000" }

    Connections {
        target: root.backend
        function onEditorChanged() {
            if (root.backend.editor.kind) root.open()
            else if (root.opened) root.close()
        }
    }
    onClosed: if (root.backend.editor.kind) root.backend.closeEditor()

    function selectedVms() {
        const result = []
        for (let i = 0; i < vmRepeater.count; i++) {
            const control = vmRepeater.itemAt(i)
            if (control && control.checked) result.push(control.vmName)
        }
        return result
    }

    ScrollView {
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: 12
            anchors.margins: 24

            Label { Layout.leftMargin: 24; text: root.backend.editor.kind === "vm" ? "NEW VM" : root.backend.editor.kind === "rules" ? "RULE" : "CREDENTIAL"; color: "#72e0bd"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.3 }
            Label { Layout.leftMargin: 24; text: root.backend.editor.mode === "edit" ? "Edit " + (root.backend.editor.name || "") : root.backend.editor.kind === "vm" ? "Create an agent VM" : "Create " + (root.backend.editor.kind === "rules" ? "a rule" : "a credential"); font.pixelSize: 22; font.bold: true; Layout.fillWidth: true; wrapMode: Text.WrapAnywhere }

            // VM creation form and live job
            ColumnLayout {
                visible: root.backend.editor.kind === "vm" && (root.backend.job.state === "idle" || root.backend.job.kind !== "vm_create")
                Layout.fillWidth: true
                Layout.leftMargin: 24
                Layout.rightMargin: 24
                spacing: 8
                TextField { id: vmName; Layout.fillWidth: true; placeholderText: "VM name (lowercase letters, digits and hyphens)" }
                Label { visible: !!root.backend.errorFields.name; text: root.backend.errorFields.name || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                RowLayout {
                    SpinBox { id: vmCpus; from: 1; to: 128; value: 4; editable: true }
                    Label { text: "CPUs"; color: "#8793a5" }
                    Item { Layout.fillWidth: true }
                    Label { text: "Ubuntu"; color: "#8793a5" }
                    TextField { id: vmImage; text: "ubuntu:24.04"; Layout.preferredWidth: 145 }
                }
                Label { visible: !!root.backend.errorFields.image; text: root.backend.errorFields.image || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                RowLayout {
                    TextField { id: vmMemory; text: "8GiB"; placeholderText: "Memory"; Layout.fillWidth: true }
                    TextField { id: vmDisk; text: "40GiB"; placeholderText: "Root disk"; Layout.fillWidth: true }
                }
                RowLayout {
                    Layout.fillWidth: true
                    Label { text: "Optional SSH public key"; color: "#8793a5"; font.pixelSize: 11 }
                    Item { Layout.fillWidth: true }
                }
                TextField { id: vmSshKey; Layout.fillWidth: true; placeholderText: "Use the host's default key if empty"; font.family: "monospace" }
                Label { visible: !!root.backend.errorFields.ssh_public_key; text: root.backend.errorFields.ssh_public_key || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                Label { visible: root.backend.errorMessage !== ""; text: root.backend.errorMessage; color: "#ff7b7b"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    JettyButton { text: "Cancel"; flat: true; onClicked: root.backend.closeEditor() }
                    JettyButton {
                        text: "Create VM"
                        highlighted: true
                        enabled: root.backend.job.state !== "submitting" && root.backend.job.state !== "running" && root.backend.job.state !== "queued"
                        onClicked: root.backend.startCreate({name: vmName.text, cpus: vmCpus.value, memory: vmMemory.text, disk: vmDisk.text, image: vmImage.text, ssh_public_key: vmSshKey.text})
                    }
                }
            }
            ColumnLayout {
                visible: root.backend.editor.kind === "vm" && root.backend.job.kind === "vm_create" && ["submitting", "queued", "running", "failed"].includes(root.backend.job.state)
                Layout.fillWidth: true
                Layout.leftMargin: 24
                Layout.rightMargin: 24
                JobStepper { Layout.fillWidth: true; job: root.backend.job; failureNote: "The VM may exist even though setup did not finish. Check the VM list before trying again." }
                RowLayout {
                    visible: root.backend.job.state === "failed"
                    JettyButton { text: "Close"; flat: true; onClicked: root.backend.closeEditor() }
                    Item { Layout.fillWidth: true }
                    JettyButton { text: "Try again"; highlighted: true; onClicked: root.backend.dismissJob() }
                }
            }
            ColumnLayout {
                visible: root.backend.editor.kind === "vm" && root.backend.job.kind === "vm_create" && root.backend.job.state === "done"
                Layout.fillWidth: true
                Layout.leftMargin: 24
                Layout.rightMargin: 24
                spacing: 13
                Pane {
                    Layout.fillWidth: true
                    Material.background: "#16332b"
                    Label { width: parent.width; text: "VM created successfully at " + ((root.backend.job.result || {}).ip_address || "unknown address") + "."; color: "#72e0bd"; wrapMode: Text.Wrap }
                }
                Label {
                    Layout.fillWidth: true
                    text: "It is not set up yet. Use your agent to prepare it: the agent inspects your project, installs its toolchain and coding-agent harnesses, and adds the access rules it needs."
                    color: "#edf2f7"
                    wrapMode: Text.Wrap
                }
                AgentHandoff { Layout.fillWidth: true; backend: root.backend; vmName: (root.backend.job.result || {}).name || "" }
                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    JettyButton { text: "Done"; highlighted: true; onClicked: { root.backend.dismissJob(); root.backend.closeEditor() } }
                }
            }

            // Credential editor
            ColumnLayout {
                visible: root.backend.editor.kind === "credentials"
                Layout.fillWidth: true
                Layout.leftMargin: 24
                Layout.rightMargin: 24
                spacing: 8
                ComboBox {
                    id: credentialQuickAdd
                    Layout.fillWidth: true
                    visible: root.backend.editor.mode === "create"
                    model: ["Quick add a provider…"].concat(root.backend.quickAdds.map(entry => entry.display_name))
                    currentIndex: 0
                    onActivated: index => {
                        if (index <= 0) return
                        const entry = root.backend.quickAdd(index - 1)
                        credentialName.text = entry.key
                        credentialCommand.text = entry.command
                        credentialTtl.value = entry.ttl_seconds
                    }
                }
                TextField { id: credentialName; Layout.fillWidth: true; text: root.backend.editor.name || ""; placeholderText: "Credential name"; enabled: root.backend.editor.mode === "create" }
                Label { visible: !!root.backend.errorFields.name; text: root.backend.errorFields.name || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                Label { text: "Provider command"; color: "#8793a5"; font.pixelSize: 11 }
                TextArea { id: credentialCommand; Layout.fillWidth: true; text: root.backend.editor.command || ""; placeholderText: "Command that prints a credential to stdout"; wrapMode: TextEdit.Wrap; selectByMouse: true; implicitHeight: 180 }
                Label { visible: !!root.backend.errorFields.command; text: root.backend.errorFields.command || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                RowLayout {
                    Label { text: "Cache TTL in seconds"; color: "#8793a5" }
                    SpinBox { id: credentialTtl; from: 0; to: 10000000; value: root.backend.editor.ttl_seconds === undefined ? 300 : root.backend.editor.ttl_seconds; editable: true }
                    Label { text: "0 disables caching"; color: "#8793a5"; font.pixelSize: 11 }
                }
                Label { visible: !!root.backend.errorFields.ttl_seconds; text: root.backend.errorFields.ttl_seconds || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                Label { visible: root.backend.errorMessage !== ""; text: root.backend.errorMessage; color: "#ff7b7b"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    JettyButton { text: "Cancel"; flat: true; onClicked: root.backend.closeEditor() }
                    JettyButton { text: "Save credential"; highlighted: true; onClicked: root.backend.saveCredential({name: credentialName.text, command: credentialCommand.text, ttl_seconds: credentialTtl.value}) }
                }
            }

            // Rule editor
            ColumnLayout {
                id: ruleForm
                visible: root.backend.editor.kind === "rules"
                Layout.fillWidth: true
                Layout.leftMargin: 24
                Layout.rightMargin: 24
                spacing: 8
                TextField { id: ruleName; Layout.fillWidth: true; text: root.backend.editor.name || ""; placeholderText: "Rule name"; enabled: root.backend.editor.mode === "create" }
                Label { visible: !!root.backend.errorFields.name; text: root.backend.errorFields.name || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                RowLayout {
                    Label { text: "Priority"; color: "#8793a5" }
                    SpinBox { id: rulePriority; from: 0; to: 10000000; value: root.backend.editor.priority === undefined ? 100 : root.backend.editor.priority; editable: true }
                    Item { Layout.fillWidth: true }
                }
                Label { visible: !!root.backend.errorFields.priority; text: root.backend.errorFields.priority || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                TextField { id: ruleHost; Layout.fillWidth: true; text: root.backend.editor.hostname || ""; placeholderText: "Exact hostname, such as api.github.com" }
                Label { visible: !!root.backend.errorFields.hostname; text: root.backend.errorFields.hostname || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                Label { text: "VM selector"; color: "#8793a5"; font.pixelSize: 11 }
                ComboBox {
                    id: selectorMode
                    Layout.fillWidth: true
                    model: ["All VMs", "Only selected VMs"]
                    currentIndex: root.backend.editor.vm_selector && root.backend.editor.vm_selector.type === "only" ? 1 : 0
                }
                Flow {
                    visible: selectorMode.currentIndex === 1
                    Layout.fillWidth: true
                    Repeater {
                        id: vmRepeater
                        model: root.backend.vmsModel
                        delegate: CheckBox {
                            required property var item
                            required property int index
                            property string vmName: item.name
                            text: vmName
                            checked: !!(root.backend.editor.vm_selector && (root.backend.editor.vm_selector.vms || []).includes(vmName))
                        }
                    }
                }
                Label { visible: !!root.backend.errorFields.vm_selector; text: root.backend.errorFields.vm_selector || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                Label { text: "Action"; color: "#8793a5"; font.pixelSize: 11 }
                ComboBox {
                    id: actionType
                    Layout.fillWidth: true
                    model: ["Allow", "Block", "Allow with credential"]
                    currentIndex: root.backend.editor.action ? (root.backend.editor.action.type === "block" ? 1 : root.backend.editor.action.type === "allow_with_credential" ? 2 : 0) : 0
                }
                ColumnLayout {
                    visible: actionType.currentIndex === 2
                    Layout.fillWidth: true
                    TextField { id: pathPrefix; Layout.fillWidth: true; text: root.backend.editor.action ? root.backend.editor.action.path_prefix || "/" : "/"; placeholderText: "Path prefix, e.g. /v1/" }
                    ComboBox {
                        id: credentialChoice
                        Layout.fillWidth: true
                        model: root.backend.credentialsModel.items
                        textRole: "name"
                        currentIndex: {
                            const value = root.backend.editor.action ? root.backend.editor.action.credential : ""
                            return root.backend.credentialsModel.items.findIndex(row => row.name === value)
                        }
                    }
                    ComboBox {
                        id: injectionType
                        Layout.fillWidth: true
                        model: ["Bearer", "Basic"]
                        currentIndex: root.backend.editor.action && root.backend.editor.action.injection && root.backend.editor.action.injection.type === "basic" ? 1 : 0
                    }
                    TextField { id: injectionUsername; Layout.fillWidth: true; visible: injectionType.currentIndex === 1; text: root.backend.editor.action && root.backend.editor.action.injection ? root.backend.editor.action.injection.username || "" : ""; placeholderText: "Basic auth username" }
                }
                Label { visible: !!root.backend.errorFields.action; text: root.backend.errorFields.action || ""; color: "#ff7b7b"; font.pixelSize: 11 }
                Label { visible: root.backend.errorMessage !== ""; text: root.backend.errorMessage; color: "#ff7b7b"; wrapMode: Text.Wrap; Layout.fillWidth: true }
                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    JettyButton { text: "Cancel"; flat: true; onClicked: root.backend.closeEditor() }
                    JettyButton {
                        text: "Save rule"
                        highlighted: true
                        onClicked: {
                            const selector = selectorMode.currentIndex === 0 ? {type: "all"} : {type: "only", vms: root.selectedVms()}
                            let action = {type: actionType.currentIndex === 0 ? "allow" : actionType.currentIndex === 1 ? "block" : "allow_with_credential"}
                            if (actionType.currentIndex === 2) {
                                const credentials = root.backend.credentialsModel.items
                                action = {type: "allow_with_credential", credential: credentialChoice.currentIndex >= 0 ? credentials[credentialChoice.currentIndex].name : "", path_prefix: pathPrefix.text, injection: injectionType.currentIndex === 0 ? {type: "bearer"} : {type: "basic", username: injectionUsername.text}}
                            }
                            root.backend.saveRule({name: ruleName.text, priority: rulePriority.value, hostname: ruleHost.text, vm_selector: selector, action: action})
                        }
                    }
                }
            }
        }
    }
}
