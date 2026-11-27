import "./style.css";
import { esc, button, inventory, vmPage, setup, jobSteps, editorForm } from "./views";
import type { ConnectionLog, Credential, Job, Port, QuickAddEntry, RequestDetail, Rule, Status, View } from "./types";

const root = document.querySelector<HTMLElement>("#app")!;
root.innerHTML = '<div id="dashboard"></div><dialog id="editor" aria-labelledby="editor-title"></dialog><dialog id="confirm" aria-labelledby="confirm-title"></dialog>';
const dashboard = document.querySelector<HTMLElement>("#dashboard")!;
const editor = document.querySelector<HTMLDialogElement>("#editor")!;
const confirmDialog = document.querySelector<HTMLDialogElement>("#confirm")!;

let view: View = "logs";
let selection = "";
let status: Status | undefined;
let rules: Rule[] = [];
let credentials: Credential[] = [];
let catalog: QuickAddEntry[] = [];
let logs: ConnectionLog[] = [];
let ports: Port[] = [];
let reversePorts: Port[] = [];
let detail: RequestDetail | undefined;
let detailError = "";
let loadError = "";
let message = "";
let messageError = false;
let messageTimer: number;
let skipSetup = false;
let lastMarkup = "";
let setupKey = "";
let activeJob: { kind: string; started: number; event: Job; busy: boolean } | undefined;
let saving = false;
let actionPending = false;

async function rpc<T = unknown>(method: string, ...args: unknown[]): Promise<T> {
  const fn = window.go?.main?.App?.[method];
  if (!fn) throw new Error("Jetty daemon connection is unavailable.");
  return (await fn(...args)) as T;
}

let refreshTask: Promise<void> | undefined;
let refreshAgain = false;
function refresh(): Promise<void> {
  if (refreshTask) {
    refreshAgain = true;
    return refreshTask;
  }
  refreshTask = (async () => {
    do {
      refreshAgain = false;
      const errors: string[] = [];
      async function load<T>(method: string, args: unknown[], apply: (value: T) => void) {
        try {
          apply(await rpc<T>(method, ...args));
        } catch (error) {
          errors.push(String(error));
        }
      }
      await Promise.all([
        load<Status>("GetStatus", [], value => {
          if (value.gateway !== "running") skipSetup = false;
          status = value;
        }),
        load<{ rules: Rule[] }>("ListRules", ["", false], value => { rules = value.rules ?? []; }),
        load<{ credentials: Credential[] }>("ListCredentials", [], value => { credentials = value.credentials ?? []; }),
        load<{ entries: QuickAddEntry[] }>("GetQuickAddCatalog", [], value => { catalog = value.entries ?? []; }),
        load<{ connections: ConnectionLog[] }>("ListRequests", ["", "", "", 100], value => { logs = value.connections ?? []; }),
        load<{ ports: Port[] }>("ListPorts", [""], value => { ports = value.ports ?? []; }),
        load<{ ports: Port[] }>("ListReversePorts", [""], value => { reversePorts = value.ports ?? []; }),
      ]);
      loadError = errors[0] ?? "";
      render();
    } while (refreshAgain);
  })().finally(() => { refreshTask = undefined; });
  return refreshTask;
}

function needsSetup() {
  if (activeJob?.kind === "gateway_setup" && activeJob.busy) return true;
  return !skipSetup && (["not_configured", "waiting_for_network"].includes(status?.state ?? "")
    || ["missing", "stopped"].includes(status?.gateway ?? ""));
}

function render() {
  const vms = [...(status?.vms ?? [])].sort((a, b) =>
    Number(b.status.toLowerCase() === "running") - Number(a.status.toLowerCase() === "running") || a.name.localeCompare(b.name));
  const globalRules = rules.filter(rule => !rule.scope);
  const vm = view === "vm" ? vms.find(item => item.name === selection) : undefined;
  const ready = status?.daemon === "running" && status?.state === "running";
  const previousForm = dashboard.querySelector<HTMLFormElement>("form[data-draft]");
  const focused = document.activeElement as HTMLInputElement | null;
  const setupFocus = focused?.name === "ssh_public_key" && focused.form?.dataset.draft === "setup";
  const caret = setupFocus ? [focused.selectionStart, focused.selectionEnd] : [];
  const scroll = new Map(Array.from(dashboard.querySelectorAll<HTMLElement>("[data-scroll]"),
    node => [node.dataset.scroll, node.scrollTop]));
  const markup = `
    <header class="topbar"><strong>JETTY</strong><div class="system">
      <span><i class="${status?.state === "failed" ? "failed" : ""}"></i> proxy ${esc(status?.state || "unknown")}</span>
      ${status?.gateway && status.gateway !== "unknown" ? `<span>gateway ${esc(status.gateway)}</span>` : ""}
      ${status?.gateway_service && status.gateway_service !== "unknown" ? `<span>${esc(status.gateway_service)}</span>` : ""}
    </div></header>
    <div class="workspace"><nav class="sidebar" aria-label="Jetty">
      <div class="nav-label">Management</div>
      ${([["logs", "≋", "Logs"], ["rules", "◇", "Global rules"], ["credentials", "▤", "Credentials"]] as const)
        .map(([key, glyph, caption]) => `<button class="nav ${key === view ? "selected" : ""}" data-view="${key}"><span>${glyph}</span>${caption}</button>`).join("")}
      <hr>${button("+", "New environment", 'data-edit="vm"', "primary wide", !ready || needsSetup() || activeJob?.busy === true)}
      <div class="vm-nav" data-scroll="navigation">${vms.map(item => `
        <button class="nav vm-nav-button ${item === vm ? "selected" : ""}" data-vm="${esc(item.name)}" title="${esc(item.name)} · ${esc(item.status)}"><i class="${item.status.toLowerCase() === "stopped" ? "stopped" : ""}"></i>${esc(item.name)}</button>`).join("")}</div>
      <div class="sidebar-status"><span>● daemon ${ready ? "ready" : "unavailable"}</span><small>LXD ${status?.lxd_available ? "available" : "unavailable"}</small></div>
    </nav>
    ${needsSetup()
      ? setup(status!, progress("gateway_setup"), activeJob?.busy === true, setupKey)
      : view === "vm"
        ? vmPage(vm, rules.filter(rule => rule.scope === selection), globalRules.length, ports.filter(port => port.vm_name === selection), reversePorts.filter(port => port.vm_name === selection), actionPending)
        : inventory(view, selection, logs, globalRules, credentials, detail, detailError, actionPending)}
    </div>`;
  const renderKey = JSON.stringify([markup, message, messageError, loadError]);
  if (renderKey === lastMarkup) return;
  lastMarkup = renderKey;
  const template = document.createElement("template");
  template.innerHTML = markup;
  const nextForm = template.content.querySelector<HTMLFormElement>("form[data-draft]");
  const retained = previousForm && nextForm && previousForm.dataset.draft === nextForm.dataset.draft
    && previousForm.dataset.draft !== "setup";
  if (retained) nextForm.replaceWith(previousForm);
  dashboard.replaceChildren(template.content);
  const notice = dashboard.querySelector(".notice");
  if (notice) {
    notice.textContent = message || loadError;
    notice.classList.toggle("error", messageError || (!message && !!loadError));
  }
  dashboard.querySelectorAll<HTMLElement>("[data-scroll]").forEach(node => {
    node.scrollTop = scroll.get(node.dataset.scroll) ?? 0;
  });
  if (retained && focused && previousForm.contains(focused)) focused.focus();
  if (setupFocus) {
    const input = dashboard.querySelector<HTMLInputElement>('input[name="ssh_public_key"]');
    input?.focus();
    if (caret[0] != null && caret[1] != null) input?.setSelectionRange(caret[0], caret[1]);
  }
}

function notify(text: string, error = false) {
  message = text;
  messageError = error;
  clearTimeout(messageTimer);
  messageTimer = window.setTimeout(() => {
    message = "";
    render();
  }, 7000);
  render();
}

function navigate(next: View, key = "") {
  view = next;
  selection = key;
  detail = undefined;
  detailError = "";
  message = "";
  dashboard.querySelectorAll<HTMLElement>("[data-scroll]:not(.vm-nav)").forEach(node => { node.scrollTop = 0; });
  render();
}

let detailRevision = 0;
async function selectLog(key: string) {
  navigate("logs", key);
  const revision = ++detailRevision;
  try {
    const result = await rpc<RequestDetail>("GetRequest", key);
    if (revision === detailRevision && view === "logs" && selection === key) detail = result;
  } catch (error) {
    if (revision === detailRevision && view === "logs" && selection === key) detailError = String(error);
  }
  render();
}

function openEditor(kind: string, name = "") {
  const scope = view === "vm" ? selection : "";
  const form = editorForm(kind, scope, rules.find(rule => rule.scope === scope && rule.name === name),
    credentials.find(credential => credential.name === name), credentials, catalog);
  editor.innerHTML = form;
  editor.showModal();
}

function ask(title: string, description: string): Promise<boolean> {
  confirmDialog.innerHTML = `<form method="dialog"><h2 id="confirm-title">${esc(title)}</h2><p>${esc(description)}</p>
    <div class="actions"><button class="flat" value="no" autofocus>No</button><button class="primary" value="yes">Yes</button></div></form>`;
  confirmDialog.returnValue = "";
  confirmDialog.showModal();
  return new Promise(resolve => confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "yes"), { once: true }));
}

async function mutate(method: string, args: unknown[], success: string, after?: () => void) {
  if (actionPending) return false;
  actionPending = true;
  render();
  try {
    await rpc(method, ...args);
    after?.();
    notify(success);
    await refresh();
    return true;
  } catch (error) {
    notify(String(error), true);
    return false;
  } finally {
    actionPending = false;
    render();
  }
}

function progress(kind: string) {
  if (activeJob?.kind !== kind) return "";
  return jobSteps(activeJob.event, Math.floor((Date.now() - activeJob.started) / 1000));
}

function renderJob() {
  if (activeJob?.kind === "gateway_setup") {
    const target = dashboard.querySelector("[data-setup-progress]");
    if (target) target.innerHTML = progress("gateway_setup");
    return;
  }
  const target = editor.querySelector<HTMLElement>("[data-create-progress]");
  if (!target || !activeJob) return;
  const form = editor.querySelector<HTMLFormElement>("form")!;
  form.hidden = true;
  target.innerHTML = progress("vm_create");
  if (!activeJob.busy) {
    const done = activeJob.event.state === "done";
    target.innerHTML += done
      ? `<p>Environment created successfully at ${esc(activeJob.event.vm?.ip_address || "unknown address")}.</p>
         <p>Use your agent to prepare the environment and add the access rules it needs.</p>
         <div class="actions">${button("✓", "Done", "data-job-done", "primary")}</div>`
      : `<div class="actions">${button("×", "Close", "data-close-editor", "flat")}${button("↻", "Try again", "data-job-retry", "primary")}</div>`;
  }
}

async function runJob(kind: string, method: string, args: unknown[]) {
  if (activeJob?.busy) return;
  activeJob = { kind, started: Date.now(), event: { kind, state: "submitting" }, busy: true };
  render();
  renderJob();
  const update = window.setInterval(() => {
    dashboard.querySelectorAll("[data-elapsed]").forEach(node => { node.textContent = elapsed(); });
    editor.querySelectorAll("[data-elapsed]").forEach(node => { node.textContent = elapsed(); });
  }, 1000);
  function elapsed() {
    return `${Math.floor((Date.now() - activeJob!.started) / 1000)} s`;
  }
  try {
    const result = await rpc<Job>(method, ...args);
    if (!["done", "failed"].includes(result.state)) throw new Error("The operation ended without a final result.");
    activeJob.event = result;
    if (result.state === "done" && kind === "vm_create" && result.vm?.name) {
      navigate("vm", result.vm.name);
    }
  } catch (error) {
    activeJob.event = { ...activeJob.event, state: "failed", error_message: String(error) };
  } finally {
    clearInterval(update);
    activeJob.busy = false;
    await refresh();
    renderJob();
  }
}

dashboard.addEventListener("click", async event => {
  const target = (event.target as HTMLElement).closest<HTMLButtonElement>("button");
  if (!target || target.disabled) return;
  const data = target.dataset;
  if (data.view) navigate(data.view as View);
  if (data.vm) navigate("vm", data.vm);
  if (data.refresh !== undefined) void refresh();
  if (data.log) void selectLog(data.log);
  if (data.select) navigate(view, data.select);
  if (data.edit) openEditor(data.edit, data.name);
  if (data.dashboard !== undefined) {
    skipSetup = true;
    render();
  }
  if (data.direction !== undefined) {
    const form = target.form!;
    const reverse = form.dataset.reverse !== "true";
    form.dataset.reverse = String(reverse);
    target.textContent = reverse ? "←" : "→";
    target.setAttribute("aria-pressed", String(reverse));
    const vmPort = form.elements.namedItem("vm_port") as HTMLInputElement;
    const hostPort = form.elements.namedItem("host_port") as HTMLInputElement;
    vmPort.required = !reverse;
    vmPort.min = reverse ? "1024" : "1";
    hostPort.required = reverse;
    hostPort.min = reverse ? "1" : "1024";
  }
  if (data.vmAction) {
    const name = selection;
    const action = data.vmAction;
    const description = action === "delete"
      ? "This permanently removes the VM, its LXD instance, and its rules."
      : `Jetty will ${action} this VM.`;
    if (await ask(`${action[0].toUpperCase() + action.slice(1)} ${name}?`, description)) {
      if (action === "delete") await mutate("DeleteVm", [name], `Deleted ${name}.`, () => navigate("logs"));
      else await mutate("VmAction", [name, action], `Requested ${action} for ${name}.`);
    }
  }
  if (data.deleteRule || data.deleteCredential) {
    const name = data.deleteRule || data.deleteCredential!;
    const scope = view === "vm" ? selection : "";
    if (await ask(`Delete ${name}?`, `Delete this ${data.deleteRule ? "rule" : "credential"}?`)) {
      await mutate(data.deleteRule ? "DeleteRule" : "DeleteCredential", data.deleteRule ? [name, scope] : [name],
        `Deleted ${name}.`, () => { if (selection === name && view !== "vm") selection = ""; });
    }
  }
  if (data.clearCache) await mutate("RefreshCredential", [data.clearCache], "Credential cache cleared.");
  if (data.files !== undefined) await mutate("OpenFiles", [selection], "Opened VM files.");
  if (data.agent) {
    const name = selection;
    notify(`Opening ${target.textContent?.replace("↗", "").trim() || data.agent}…`);
    await mutate("HandoffVm", [name, data.agent], "Agent opened.");
  }
  if (data.closePort) await mutate(data.reverse ? "CloseReversePort" : "ClosePort", [selection, +data.closePort], "Port connection closed.");
  if (data.openUrl) {
    try { await rpc("OpenPortUrl", Number(data.openUrl)); }
    catch (error) { notify(String(error), true); }
  }
});

dashboard.addEventListener("input", event => {
  const target = event.target as HTMLInputElement;
  if (target.name === "ssh_public_key" && target.form?.dataset.draft === "setup") setupKey = target.value;
});

dashboard.addEventListener("change", async event => {
  if (!(event.target instanceof HTMLInputElement)) return;
  const input = event.target;
  if (!input.dataset.persist) return;
  const checked = input.checked;
  const ok = await mutate(input.dataset.reverse ? "UpdateReversePort" : "UpdatePort",
    [selection, +input.dataset.persist, checked], "Port connection updated.");
  if (!ok) input.checked = !checked;
});

dashboard.addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.target as HTMLFormElement;
  const values = new FormData(form);
  if (form.dataset.draft === "setup") {
    await runJob("gateway_setup", "SetupGateway", [setupKey.trim()]);
  } else {
    const reverse = form.dataset.reverse === "true";
    const vmPort = Number(values.get("vm_port"));
    const hostPort = Number(values.get("host_port"));
    const ok = await mutate(reverse ? "OpenReversePort" : "OpenPort",
      [selection, ...(reverse ? [hostPort, vmPort] : [vmPort, hostPort]), values.has("persistent")],
      "Port connection added.");
    if (ok) form.reset();
  }
});

editor.addEventListener("click", event => {
  const target = (event.target as HTMLElement).closest<HTMLButtonElement>("button");
  if (!target) return;
  if (target.hasAttribute("data-close-editor") || target.hasAttribute("data-job-done")) editor.close();
  if (target.hasAttribute("data-job-retry")) {
    activeJob = undefined;
    editor.querySelector("form")!.hidden = false;
    editor.querySelector("[data-create-progress]")!.replaceChildren();
    render();
  }
  if (target.hasAttribute("data-aws")) {
    const form = target.form!;
    for (const [name, value] of Object.entries({ hostname: "@aws", service: "auto", region: "auto" })) {
      (form.elements.namedItem(name) as HTMLInputElement).value = value;
    }
  }
});
editor.addEventListener("cancel", event => {
  if (saving || activeJob?.busy && activeJob.kind === "vm_create") event.preventDefault();
});
editor.addEventListener("close", () => {
  if (activeJob?.kind === "vm_create" && !activeJob.busy) activeJob = undefined;
  render();
});

editor.addEventListener("change", event => {
  const target = event.target as HTMLSelectElement;
  const form = target.form;
  if (!form) return;
  if (target.name === "quick_add") {
    const entry = catalog.find(item => item.key === target.value);
    if (entry) {
      for (const [name, value] of Object.entries({ name: entry.key, command: entry.command, ttl_seconds: entry.ttl_seconds })) {
        (form.elements.namedItem(name) as HTMLInputElement | HTMLTextAreaElement).value = String(value);
      }
    }
  }
  if (target.name === "instance_type") {
    form.querySelector<HTMLElement>("[data-container-note]")!.hidden = target.value !== "container";
    form.querySelector<HTMLButtonElement>("[type=submit]")!.textContent = target.value === "container" ? "Create container" : "Create VM";
  }
  if (target.name === "action_type" || target.name === "injection_type") {
    const values = new FormData(form);
    form.querySelector<HTMLElement>("[data-injection-fields]")!.hidden = values.get("action_type") !== "allow_with_credential";
    form.querySelectorAll<HTMLElement>("[data-injection]").forEach(field => {
      field.hidden = field.dataset.injection !== values.get("injection_type");
    });
  }
});

editor.addEventListener("submit", async event => {
  event.preventDefault();
  if (saving) return;
  const form = event.target as HTMLFormElement;
  const values = new FormData(form);
  const get = (name: string) => String(values.get(name) ?? "");
  if (form.dataset.kind === "vm") {
    await runJob("vm_create", "CreateVm", [get("name").trim(), +get("cpus"), get("memory").trim(),
      get("disk").trim(), get("image").trim(), get("instance_type"), get("ssh_public_key").trim()]);
    return;
  }
  const errorLabel = form.querySelector<HTMLElement>(".error")!;
  const buttons = form.querySelectorAll("button");
  saving = true;
  buttons.forEach(btn => { btn.disabled = true; });
  errorLabel.textContent = "";
  try {
    if (form.dataset.kind === "credentials") {
      await rpc("PutCredential", get("name").trim(), get("command"), +get("ttl_seconds"));
    } else {
      const input = Object.fromEntries(values);
      await rpc("PutRule", { ...input, name: get("name").trim(), hostname: get("hostname").trim(),
        priority: +get("priority"), scope: form.dataset.scope || "", path_prefix: get("path_prefix").trim() || "/" });
    }
    editor.close();
    notify(form.dataset.kind === "rules" ? "Rule saved." : "Credential saved.");
    await refresh();
  } catch (error) {
    errorLabel.textContent = String(error);
  } finally {
    buttons.forEach(btn => { btn.disabled = false; });
    saving = false;
  }
});

let watchRefresh: number | undefined;
window.runtime?.EventsOn("jetty:event", () => {
  if (watchRefresh !== undefined) return;
  watchRefresh = window.setTimeout(() => {
    watchRefresh = undefined;
    void refresh();
  }, 250);
});
window.runtime?.EventsOn("jetty:connection", () => { void refresh(); });
window.runtime?.EventsOn("jetty:job", (event: unknown) => {
  const job = event as Job;
  if (activeJob?.busy && activeJob.kind === job.kind) {
    activeJob.event = job;
    renderJob();
  }
});
render();
void refresh();
window.setInterval(() => { void refresh(); }, 5000);
