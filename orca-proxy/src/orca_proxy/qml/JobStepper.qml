import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ColumnLayout {
    id: root
    required property var job
    property string failureNote: ""
    spacing: 9

    RowLayout {
        Layout.fillWidth: true
        BusyIndicator {
            visible: root.job.state === "queued" || root.job.state === "submitting" || root.job.state === "running"
            running: visible
            implicitWidth: 24
            implicitHeight: 24
        }
        Label {
            Layout.fillWidth: true
            text: root.job.state === "queued" ? "Waiting to start…" : root.job.state === "submitting" ? "Starting operation…" : root.job.state === "done" ? "Complete" : root.job.state === "failed" ? "Operation failed" : "Working…"
            font.bold: true
            color: root.job.state === "failed" ? "#ff7b7b" : root.job.state === "done" ? "#72e0bd" : "#edf2f7"
        }
        Label { text: Math.floor(root.job.elapsed_seconds || 0) + " s"; color: "#8793a5"; font.family: "monospace"; font.pixelSize: 11 }
    }

    Repeater {
        model: root.job.steps || []
        delegate: RowLayout {
            required property var modelData
            Layout.fillWidth: true
            spacing: 10
            Item {
                width: 20
                height: 20
                BusyIndicator {
                    anchors.fill: parent
                    visible: modelData.state === "running"
                    running: visible
                    padding: 0
                }
                Label {
                    anchors.centerIn: parent
                    visible: modelData.state !== "running"
                    text: modelData.state === "done" ? "✓" : modelData.state === "failed" ? "✕" : "○"
                    color: modelData.state === "done" ? "#72e0bd" : modelData.state === "failed" ? "#ff7b7b" : "#556070"
                    font.bold: true
                }
            }
            Label {
                Layout.fillWidth: true
                text: modelData.label
                color: modelData.state === "pending" ? "#667386" : modelData.state === "failed" ? "#ff7b7b" : "#edf2f7"
                font.bold: modelData.state === "running"
                wrapMode: Text.Wrap
            }
        }
    }

    ProgressBar {
        Layout.fillWidth: true
        visible: root.job.state === "running" || root.job.state === "queued"
        indeterminate: true
    }
    Label {
        Layout.fillWidth: true
        visible: root.job.state === "failed"
        text: ((root.job.error || {}).message || "The operation failed.") + (root.failureNote ? "\n" + root.failureNote : "")
        color: "#ff7b7b"
        wrapMode: Text.Wrap
    }
}
