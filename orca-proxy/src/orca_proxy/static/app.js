const state = {
  activeView: "logs",
  vms: [],
  credentials: [],
  rules: [],
  connections: [],
  selectedConnectionId: null,
  selectedConnectionDetail: null,
  selectedRuleName: null,
  selectedVmName: null,
  selectedCredentialName: null,
  quickAddCatalog: [],
  ca: null,
  jettyStatus: null,
  editor: null, // { kind: "rule"|"credential"|"vm", existingName, errors: {}, banner }
  loadError: null,
};

// --- API ---

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    const err = new Error(data?.error?.message || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.code = data?.error?.code;
    err.fields = data?.error?.fields || {};
    throw err;
  }
  return data;
}

async function loadEntities() {
  const [vms, credentials, rules, ca] = await Promise.all([
    api("GET", "/api/v1/vms"),
    api("GET", "/api/v1/credentials"),
    api("GET", "/api/v1/rules"),
    api("GET", "/api/v1/ca"),
  ]);
  state.vms = vms.vms;
  try {
    const instances = await api("GET", "/api/v1/jetty/vms");
    const byName = new Map(instances.vms.map((vm) => [vm.name, vm]));
    state.vms = vms.vms.map((vm) => ({ ...vm, status: byName.get(vm.name)?.status || "unknown" }));
  } catch {
    state.vms = vms.vms.map((vm) => ({ ...vm, status: "unknown" }));
  }
  state.credentials = credentials.credentials;
  state.rules = rules.rules;
  state.ca = ca;
}

async function loadConnections() {
  const resp = await api("GET", "/api/v1/requests");
  state.connections = resp.connections;
  if (state.connections.length && !state.connections.some((c) => c.id === state.selectedConnectionId)) {
    state.selectedConnectionId = state.connections[0].id;
  }
  await loadSelectedConnectionDetail();
}

async function loadSelectedConnectionDetail() {
  if (state.selectedConnectionId === null) {
    state.selectedConnectionDetail = null;
    return;
  }
  state.selectedConnectionDetail = await api("GET", `/api/v1/requests/${state.selectedConnectionId}`);
}

async function loadQuickAddCatalog() {
  const resp = await fetch("quick-add-catalog.json");
  state.quickAddCatalog = await resp.json();
}

// --- helpers ---

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function actionClass(type) {
  if (type === "block" || type === "block_rule") return "action-block";
  if (type === "allow_with_credential" || type === "allow_credential") return "action-credential";
  return "action-allow";
}

function statusBadge(status, tone) {
  return `<span class="badge ${tone}"><span class="dot"></span>${esc(status)}</span>`;
}

function credentialStatusTone(status) {
  if (status === "valid") return "ok";
  if (status === "error") return "danger";
  return "warn";
}

function vmSelectorLabel(selector) {
  return selector.type === "all" ? "* all VMs" : selector.vms.join(", ");
}

function actionLabel(action) {
  return { allow: "Allow", block: "Block", allow_with_credential: "Allow-with-credential" }[action.type];
}

function rulesReferencingVm(name) {
  return state.rules.filter((r) => r.vm_selector.type === "all" || r.vm_selector.vms.includes(name)).length;
}

function formatTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString();
  } catch {
    return iso;
  }
}

function navButton(view, label, count) {
  const active = state.activeView === view ? "active" : "";
  return `<button class="${active}" data-view="${view}"><span>${label}</span><span class="count">${count}</span></button>`;
}

// --- tables ---

function vmsTable() {
  if (!state.vms.length) return `<div class="empty">No VMs registered yet.</div>`;
  return `<table><thead><tr><th>Name</th><th>Status</th><th>IP address</th><th>Rules</th><th>Actions</th></tr></thead><tbody>${state.vms
    .map(
      (vm) =>
        `<tr data-select data-vm="${esc(vm.name)}" class="${state.selectedVmName === vm.name ? "selected" : ""}"><td><strong>${esc(vm.name)}</strong></td><td>${statusBadge(vm.status || "unknown", vm.status === "Running" ? "ok" : "warn")}</td><td class="mono">${esc(vm.ip_address)}</td><td>${rulesReferencingVm(vm.name)}</td><td><button class="button small" data-vm-action="${esc(vm.name)}" data-action="${vm.status === "Running" ? "stop" : "start"}">${vm.status === "Running" ? "Stop" : "Start"}</button> <button class="button small" data-vm-action="${esc(vm.name)}" data-action="restart">Restart</button></td></tr>`
    )
    .join("")}</tbody></table>`;
}

function credentialsTable() {
  if (!state.credentials.length) return `<div class="empty">No Credentials yet.</div>`;
  return `<table><thead><tr><th>Name</th><th>Live status</th><th>Command</th><th>TTL</th></tr></thead><tbody>${state.credentials
    .map(
      (c) =>
        `<tr data-select data-credential="${esc(c.name)}" class="${state.selectedCredentialName === c.name ? "selected" : ""}"><td><strong>${esc(c.name)}</strong></td><td>${statusBadge(c.status, credentialStatusTone(c.status))}</td><td><code class="mono">${esc(c.command)}</code></td><td class="mono tiny">${c.ttl_seconds === 0 ? "no cache" : `${c.ttl_seconds}s`}</td></tr>`
    )
    .join("")}</tbody></table>`;
}

function rulesTable() {
  if (!state.rules.length) return `<div class="empty">No Rules yet.</div>`;
  const sorted = [...state.rules].sort((a, b) => a.priority - b.priority);
  return `<table><thead><tr><th>Priority</th><th>VM selector</th><th>Exact host</th><th>Action</th><th>Path / credential</th></tr></thead><tbody>${sorted
    .map(
      (rule) => `<tr data-select data-rule="${esc(rule.name)}" class="${state.selectedRuleName === rule.name ? "selected" : ""}">
    <td class="mono">${rule.priority}</td><td>${esc(vmSelectorLabel(rule.vm_selector))}</td><td class="mono">${esc(rule.hostname)}</td>
    <td class="${actionClass(rule.action.type)}">${esc(actionLabel(rule.action))}</td>
    <td>${rule.action.type === "allow_with_credential" ? `<span class="mono">${esc(rule.action.path_prefix)}</span><br><span class="tiny muted">${esc(rule.action.injection.type)} · ${esc(rule.action.credential)}</span>` : `<span class="muted">hostname-level</span>`}</td>
  </tr>`
    )
    .join("")}</tbody></table>`;
}

function connectionsTable() {
  if (!state.connections.length) return `<div class="empty">No requests logged yet.</div>`;
  return `<table><thead><tr><th>Time</th><th>VM</th><th>Destination</th><th>Decision</th></tr></thead><tbody>${state.connections
    .map((c) => {
      const decision = c.intercepted ? "Intercepted" : actionLabel({ type: c.outcome === "block_rule" ? "block" : c.outcome === "allow_rule" ? "allow" : "allow" });
      const label = c.intercepted ? "intercepted" : c.outcome;
      return `<tr data-select data-connection="${c.id}" class="${state.selectedConnectionId === c.id ? "selected" : ""}"><td class="mono">${formatTime(c.started_at)}</td><td>${esc(c.vm_name)}</td><td><span class="mono">${esc(c.destination_hostname || c.destination_ip)}</span><div class="tiny muted mono">${c.destination_port}</div></td><td class="${actionClass(label)}">${esc(label)}</td></tr>`;
    })
    .join("")}</tbody></table>`;
}

// --- inspectors ---

function vmInspector() {
  const vm = state.vms.find((v) => v.name === state.selectedVmName);
  if (!vm) return `<div class="eyebrow">Selection inspector</div><p class="muted">Select a VM to inspect it.</p>`;
  return `<div class="inspector-header"><div><div class="eyebrow">VM</div><h2 style="margin:6px 0 3px">${esc(vm.name)}</h2></div><button class="button danger" data-delete-vm="${esc(vm.name)}">Delete</button></div>
    <dl class="kv"><dt>Status</dt><dd>${statusBadge(vm.status || "unknown", vm.status === "Running" ? "ok" : "warn")}</dd><dt>IP address</dt><dd class="mono">${esc(vm.ip_address)}</dd><dt>Rules</dt><dd>${rulesReferencingVm(vm.name)}</dd><dt>Registered</dt><dd class="tiny">${esc(vm.created_at || "—")}</dd></dl>
    <div class="inspector-section"><button class="button" data-vm-action="${esc(vm.name)}" data-action="${vm.status === "Running" ? "stop" : "start"}">${vm.status === "Running" ? "Stop" : "Start"}</button> <button class="button" data-vm-action="${esc(vm.name)}" data-action="restart">Restart</button></div>`;
}

function credentialInspector() {
  const c = state.credentials.find((item) => item.name === state.selectedCredentialName);
  if (!c) return `<div class="eyebrow">Selection inspector</div><p class="muted">Select a Credential to inspect it.</p>`;
  return `<div class="inspector-header"><div><div class="eyebrow">Credential</div><h2 style="margin:6px 0 3px">${esc(c.name)}</h2></div><button class="button danger" data-delete-credential="${esc(c.name)}">Delete</button></div>
    <dl class="kv"><dt>Status</dt><dd>${statusBadge(c.status, credentialStatusTone(c.status))}</dd><dt>Command</dt><dd class="mono">${esc(c.command)}</dd><dt>TTL</dt><dd>${c.ttl_seconds === 0 ? "no cache" : `${c.ttl_seconds}s`}</dd>${c.failure_category ? `<dt>Last failure</dt><dd>${esc(c.failure_category)}</dd>` : ""}</dl>
    <p class="tiny muted" style="margin-top:14px">The Credential Value itself is never exposed here or anywhere in the API — only this live status.</p>`;
}

function ruleInspector() {
  const rule = state.rules.find((r) => r.name === state.selectedRuleName);
  if (!rule) return `<div class="eyebrow">Selection inspector</div><p class="muted">Select a Rule to inspect it.</p>`;
  return `<div class="inspector-header"><div><div class="eyebrow">Rule priority</div><h2 style="margin:5px 0">${rule.priority}</h2></div><div style="display:flex;gap:6px"><button class="button" data-edit-rule="${esc(rule.name)}">Edit</button><button class="button danger" data-delete-rule="${esc(rule.name)}">Delete</button></div></div>
    <dl class="kv"><dt>Name</dt><dd class="mono">${esc(rule.name)}</dd><dt>VM selector</dt><dd>${esc(vmSelectorLabel(rule.vm_selector))}</dd><dt>Exact host</dt><dd class="mono">${esc(rule.hostname)}</dd><dt>Action</dt><dd class="${actionClass(rule.action.type)}">${esc(actionLabel(rule.action))}</dd>${rule.action.type === "allow_with_credential" ? `<dt>Path prefix</dt><dd class="mono">${esc(rule.action.path_prefix)}</dd><dt>Injection</dt><dd>${esc(rule.action.injection.type)}</dd><dt>Credential</dt><dd>${esc(rule.action.credential)}</dd>` : ""}</dl>`;
}

function traceList(trace) {
  if (!trace.length) return "";
  return `<div class="inspector-section"><div class="eyebrow">Rule evaluation trace</div><ol class="decision-trace">${trace
    .map(
      (t, i) =>
        `<li><span class="trace-index">${i + 1}</span><span>priority ${t.priority} (<span class="mono">${esc(t.rule_name)}</span>, ${esc(t.action_type)}) → ${esc(t.result)}</span></li>`
    )
    .join("")}</ol></div>`;
}

function headersBlock(headers) {
  if (!headers.length) return "";
  return `<div class="inspector-section"><div class="eyebrow">Headers · redacted</div><div class="code" style="margin-top:10px">${headers.map((h) => `${esc(h.name)}: ${esc(h.value)}`).join("\n")}</div></div>`;
}

function httpRequestBlock(req) {
  return `<div class="inspector-section"><div class="kv"><dt>Request</dt><dd class="mono">${esc(req.method)} ${esc(req.path)}</dd><dt>Outcome</dt><dd class="${actionClass(req.outcome)}">${esc(req.outcome)}</dd><dt>Status</dt><dd>${req.status ?? "—"} ${req.status_origin ? `<span class="tiny muted">(${esc(req.status_origin)})</span>` : ""}</dd><dt>Latency</dt><dd>${req.latency_ms ?? "—"}ms</dd>${req.matched_credential ? `<dt>Credential</dt><dd>${esc(req.matched_credential)}</dd>` : ""}</div>
    ${traceList(req.trace)}
    ${headersBlock(req.headers)}</div>`;
}

function connectionInspector() {
  const conn = state.selectedConnectionDetail;
  if (!conn) return `<div class="eyebrow">Selection inspector</div><p class="muted">Select a request to inspect it.</p>`;
  const decisionLabel = conn.intercepted ? "Intercepted" : conn.outcome;
  return `<div class="inspector-header"><div><div class="eyebrow">Connection ${conn.id}</div><h2 style="margin:6px 0 3px" class="${actionClass(decisionLabel)}">${esc(decisionLabel)}</h2><div class="muted tiny mono">${esc(conn.started_at)}${conn.duration_ms !== null ? ` · ${conn.duration_ms}ms` : ""}</div></div></div>
    <dl class="kv"><dt>VM</dt><dd>${esc(conn.vm_name)}</dd><dt>Destination</dt><dd class="mono">${esc(conn.destination_hostname || "—")} (${esc(conn.destination_ip)}:${conn.destination_port})</dd><dt>SNI</dt><dd>${conn.sni_present ? "present" : "absent"}</dd><dt>ECH</dt><dd>${conn.ech_present ? "present" : "absent"}</dd>${conn.matched_rule ? `<dt>Matched Rule</dt><dd>priority ${conn.matched_rule.priority} (${esc(conn.matched_rule.name)})</dd>` : ""}${conn.intercepted_by_rule ? `<dt>Intercepted by</dt><dd>priority ${conn.intercepted_by_rule.priority} (${esc(conn.intercepted_by_rule.name)})</dd>` : ""}</dl>
    ${conn.http_requests.length ? `<div class="inspector-section"><div class="eyebrow">HTTP requests (${conn.http_requests.length})</div></div>${conn.http_requests.map(httpRequestBlock).join("")}` : conn.intercepted ? `<div class="inspector-section muted tiny">No HTTP requests logged on this connection yet.</div>` : ""}`;
}

// --- editor drawer ---

function fieldError(errors, name) {
  return errors && errors[name] ? `<div class="field-error">${esc(errors[name])}</div>` : "";
}

function fieldClass(errors, name) {
  return errors && errors[name] ? "field has-error" : "field";
}

function ruleEditorForm() {
  const editor = state.editor;
  const existing = editor.existingName ? state.rules.find((r) => r.name === editor.existingName) : null;
  const errors = editor.errors || {};
  const values = editor.values;
  const isCredentialAction = values.actionType === "allow_with_credential";
  return `<form class="drawer" id="entity-form">
    <div class="drawer-head"><div><div class="eyebrow">${existing ? "Edit Rule" : "New Rule"}</div><h2 style="margin:6px 0">${existing ? esc(existing.name) : "Add to policy"}</h2><p class="section-subtitle">First matching priority wins. Hostnames are exact in v1.</p></div><button type="button" class="icon-button" data-close-editor>✕</button></div>
    ${editor.banner ? `<div class="banner">${esc(editor.banner)}</div>` : ""}
    <div class="form-grid">
      <div class="${fieldClass(errors, "name")}"><label for="f-name">Name</label><input id="f-name" name="name" required value="${esc(values.name)}" ${existing ? "disabled" : ""}>${fieldError(errors, "name")}</div>
      <div class="${fieldClass(errors, "priority")}"><label for="f-priority">Unique priority · lowest first</label><input id="f-priority" name="priority" type="number" required value="${values.priority}">${fieldError(errors, "priority")}</div>
      <div class="${fieldClass(errors, "vm_selector")}"><label>VM selector</label><div class="checkboxes"><label><input type="checkbox" name="vm" value="*" ${values.vms.includes("*") ? "checked" : ""}> * all current + future</label>${state.vms.map((vm) => `<label><input type="checkbox" name="vm" value="${esc(vm.name)}" ${values.vms.includes(vm.name) ? "checked" : ""}> ${esc(vm.name)}</label>`).join("")}</div>${fieldError(errors, "vm_selector")}</div>
      <div class="${fieldClass(errors, "hostname")}"><label for="f-host">Exact host</label><input id="f-host" name="hostname" required placeholder="api.github.com" value="${esc(values.hostname)}">${fieldError(errors, "hostname")}</div>
      <div class="${fieldClass(errors, "action")}"><label for="f-action">Terminal action</label><select id="f-action" name="actionType"><option value="allow" ${values.actionType === "allow" ? "selected" : ""}>Allow</option><option value="block" ${values.actionType === "block" ? "selected" : ""}>Block</option><option value="allow_with_credential" ${values.actionType === "allow_with_credential" ? "selected" : ""}>Allow-with-credential</option></select>${fieldError(errors, "action")}</div>
      <div id="credential-fields" style="display:${isCredentialAction ? "grid" : "none"};gap:15px">
        <div class="field"><label for="f-path">Normalized path prefix</label><input id="f-path" name="pathPrefix" value="${esc(values.pathPrefix)}" placeholder="/repos/cau777"><small>Matches the identical path or descendants at a "/" boundary; query strings are ignored.</small></div>
        <div class="field"><label for="f-credential">Credential</label><select id="f-credential" name="credential">${state.credentials.map((c) => `<option value="${esc(c.name)}" ${c.name === values.credential ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></div>
        <div class="field"><label for="f-injection">Injection type</label><select id="f-injection" name="injectionType"><option value="bearer" ${values.injectionType === "bearer" ? "selected" : ""}>Bearer</option><option value="basic" ${values.injectionType === "basic" ? "selected" : ""}>Basic</option></select></div>
        <div class="field" id="username-field" style="display:${values.injectionType === "basic" ? "grid" : "none"}"><label for="f-username">Basic username</label><input id="f-username" name="username" value="${esc(values.username)}"></div>
      </div>
    </div>
    <div class="form-actions"><button type="button" class="button" data-close-editor>Cancel</button><button type="submit" class="button primary">Save</button></div>
  </form>`;
}

function credentialEditorForm() {
  const editor = state.editor;
  const errors = editor.errors || {};
  const values = editor.values;
  return `<form class="drawer" id="entity-form">
    <div class="drawer-head"><div><div class="eyebrow">New Credential</div><h2 style="margin:6px 0">Add a Credential</h2></div><button type="button" class="icon-button" data-close-editor>✕</button></div>
    ${editor.banner ? `<div class="banner">${esc(editor.banner)}</div>` : ""}
    <div class="quick-add-row">${state.quickAddCatalog.map((item) => `<button type="button" class="button" data-quick-add="${esc(item.key)}">${esc(item.display_name)}</button>`).join("")}</div>
    <div class="form-grid">
      <div class="${fieldClass(errors, "name")}"><label for="f-name">Name</label><input id="f-name" name="name" required value="${esc(values.name)}">${fieldError(errors, "name")}</div>
      <div class="${fieldClass(errors, "command")}"><label for="f-command">Command</label><input id="f-command" name="command" required value="${esc(values.command)}" class="mono">${fieldError(errors, "command")}</div>
      <div class="${fieldClass(errors, "ttl_seconds")}"><label for="f-ttl">TTL seconds (0 disables caching)</label><input id="f-ttl" name="ttlSeconds" type="number" required value="${values.ttlSeconds}">${fieldError(errors, "ttl_seconds")}</div>
    </div>
    <div class="form-actions"><button type="button" class="button" data-close-editor>Cancel</button><button type="submit" class="button primary">Save</button></div>
  </form>`;
}

function vmEditorForm() {
  const editor = state.editor;
  const errors = editor.errors || {};
  const values = editor.values;
  return `<form class="drawer" id="entity-form">
    <div class="drawer-head"><div><div class="eyebrow">New VM</div><h2 style="margin:6px 0">Create an agent VM</h2><p class="section-subtitle">Jetty launches an isolated LXD virtual machine and registers it with the proxy.</p></div><button type="button" class="icon-button" data-close-editor>✕</button></div>
    ${editor.banner ? `<div class="banner">${esc(editor.banner)}</div>` : ""}
    <div class="form-grid">
      <div class="${fieldClass(errors, "name")}"><label for="f-name">Name</label><input id="f-name" name="name" required value="${esc(values.name)}">${fieldError(errors, "name")}</div>
      <div class="${fieldClass(errors, "cpus")}"><label for="f-cpus">CPUs</label><input id="f-cpus" name="cpus" type="number" min="1" max="128" required value="${esc(values.cpus)}">${fieldError(errors, "cpus")}</div>
      <div class="${fieldClass(errors, "memory")}"><label for="f-memory">Memory</label><input id="f-memory" name="memory" required value="${esc(values.memory)}" placeholder="8GiB">${fieldError(errors, "memory")}</div>
      <div class="${fieldClass(errors, "disk")}"><label for="f-disk">Root disk</label><input id="f-disk" name="disk" required value="${esc(values.disk)}" placeholder="40GiB">${fieldError(errors, "disk")}</div>
      <div class="${fieldClass(errors, "image")}"><label for="f-image">Ubuntu image alias</label><input id="f-image" name="image" required value="${esc(values.image)}">${fieldError(errors, "image")}</div>
      <div class="field"><label for="f-ssh-key">SSH public key</label><textarea id="f-ssh-key" name="sshPublicKey" rows="3" placeholder="Optional if a standard ~/.ssh/*.pub key exists">${esc(values.sshPublicKey)}</textarea><small>The public key is copied to the VM. The private key stays on this computer.</small></div>
    </div>
    <div class="form-actions"><button type="button" class="button" data-close-editor>Cancel</button><button type="submit" class="button primary">Create VM</button></div>
  </form>`;
}

function editorDrawer() {
  if (!state.editor) return "";
  const form = { rule: ruleEditorForm, credential: credentialEditorForm, vm: vmEditorForm }[state.editor.kind]();
  return `<div class="scrim">${form}</div>`;
}

// --- top-level render ---

function viewTitle() {
  return {
    rules: ["Rules", "Unique priority is the entire ordering. Lower numbers run first."],
    credentials: ["Credentials", "Commands run on the host; only cached status is ever exposed."],
    vms: ["Virtual machines", "Create and manage isolated LXD agents."],
    logs: ["Request logs", "Every decision, including passthrough and default Allow."],
  }[state.activeView];
}

function currentViewTable() {
  return { rules: rulesTable, credentials: credentialsTable, vms: vmsTable, logs: connectionsTable }[state.activeView]();
}

function currentInspector() {
  return { rules: ruleInspector, credentials: credentialInspector, vms: vmInspector, logs: connectionInspector }[state.activeView]();
}

function newButtonFor(view) {
  const label = { rules: "New Rule", credentials: "New Credential", vms: "New VM" }[view];
  return label ? `<button class="button primary" data-new="${view}">${label}</button>` : "";
}

function render() {
  const root = document.querySelector("#app");
  const title = viewTitle();
  const count = state[state.activeView === "logs" ? "connections" : state.activeView].length;
  const toolbar =
    state.activeView === "logs"
      ? `<div class="a-toolbar"><input class="search" placeholder="Filter…" disabled><button class="button" data-refresh-logs>Refresh</button></div>`
      : `<div class="a-toolbar"><input class="search" placeholder="Filter ${title[0].toLowerCase()}…" disabled><button class="button" disabled>Filters</button></div>`;

  root.innerHTML = `<div class="a-shell">
    <aside class="a-sidebar"><div class="brand"><span class="brand-mark"></span>orca-proxy</div><nav class="a-nav">${navButton("logs", "Logs", state.connections.length)}${navButton("rules", "Rules", state.rules.length)}${navButton("credentials", "Credentials", state.credentials.length)}${navButton("vms", "VMs", state.vms.length)}</nav></aside>
    <main class="a-main"><header class="a-main-header"><div><div class="eyebrow">Inventory</div><h1 class="section-title">${title[0]} <span class="count">${count}</span></h1><p class="section-subtitle">${title[1]}</p></div>${newButtonFor(state.activeView)}</header>
      ${state.loadError ? `<div class="banner">${esc(state.loadError)}</div>` : ""}
      ${state.jettyStatus && state.jettyStatus.state !== "running" ? `<div class="banner info">Jetty status: ${esc(state.jettyStatus.state)} · <a href="/setup">Open setup</a></div>` : ""}
      ${toolbar}<div class="a-table-wrap">${currentViewTable()}</div></main>
    <aside class="a-inspector">${currentInspector()}</aside>
  </div>${editorDrawer()}`;

  bind();
}

// --- actions ---

function setView(view) {
  state.activeView = view;
  render();
}

async function selectConnection(id) {
  state.selectedConnectionId = Number(id);
  await loadSelectedConnectionDetail();
  render();
}

function selectRule(name) {
  state.selectedRuleName = name;
  render();
}

function selectVm(name) {
  state.selectedVmName = name;
  render();
}

function selectCredential(name) {
  state.selectedCredentialName = name;
  render();
}

function openRuleEditor(name = null) {
  const existing = name ? state.rules.find((r) => r.name === name) : null;
  state.editor = {
    kind: "rule",
    existingName: name,
    errors: {},
    banner: null,
    values: existing
      ? {
          name: existing.name,
          priority: existing.priority,
          vms: existing.vm_selector.type === "all" ? ["*"] : existing.vm_selector.vms,
          hostname: existing.hostname,
          actionType: existing.action.type,
          pathPrefix: existing.action.path_prefix || "/",
          credential: existing.action.credential || (state.credentials[0]?.name ?? ""),
          injectionType: existing.action.injection?.type || "bearer",
          username: existing.action.injection?.username || "",
        }
      : {
          name: "",
          priority: (Math.max(0, ...state.rules.map((r) => r.priority)) || 0) + 10,
          vms: ["*"],
          hostname: "",
          actionType: "allow",
          pathPrefix: "/",
          credential: state.credentials[0]?.name ?? "",
          injectionType: "bearer",
          username: "",
        },
  };
  render();
}

function openCredentialEditor() {
  state.editor = {
    kind: "credential",
    existingName: null,
    errors: {},
    banner: null,
    values: { name: "", command: "", ttlSeconds: 300 },
  };
  render();
}

function openVmEditor() {
  state.editor = {
    kind: "vm",
    existingName: null,
    errors: {},
    banner: null,
    values: { name: "", cpus: 4, memory: "8GiB", disk: "40GiB", image: "ubuntu:24.04", sshPublicKey: "" },
  };
  render();
}

function closeEditor() {
  state.editor = null;
  render();
}

async function deleteEntity(kind, name) {
  if (!confirm(`Delete ${kind} "${name}"?`)) return;
  try {
    const path = kind === "vm" ? `/api/v1/jetty/vms/${encodeURIComponent(name)}` : `/api/v1/${kind}s/${encodeURIComponent(name)}`;
    await api("DELETE", path);
    await loadEntities();
  } catch (err) {
    alert(`Could not delete: ${err.message}`);
  }
  render();
}

async function submitRuleForm(form) {
  const data = new FormData(form);
  const name = data.get("name");
  let vms = data.getAll("vm");
  if (vms.includes("*")) vms = null; // signal "all"
  const actionType = data.get("actionType");
  const body = {
    priority: Number(data.get("priority")),
    vm_selector: vms === null ? { type: "all" } : { type: "only", vms: vms.length ? vms : [state.vms[0]?.name].filter(Boolean) },
    hostname: data.get("hostname"),
    action:
      actionType === "allow_with_credential"
        ? {
            type: "allow_with_credential",
            credential: data.get("credential"),
            path_prefix: data.get("pathPrefix"),
            injection:
              data.get("injectionType") === "basic"
                ? { type: "basic", username: data.get("username") }
                : { type: "bearer" },
          }
        : { type: actionType },
  };
  await api("PUT", `/api/v1/rules/${encodeURIComponent(name)}`, body);
  await loadEntities();
  state.selectedRuleName = name;
}

async function submitCredentialForm(form) {
  const data = new FormData(form);
  const name = data.get("name");
  await api("PUT", `/api/v1/credentials/${encodeURIComponent(name)}`, {
    command: data.get("command"),
    ttl_seconds: Number(data.get("ttlSeconds")),
  });
  await loadEntities();
  state.selectedCredentialName = name;
}

async function submitVmForm(form) {
  const data = new FormData(form);
  const name = data.get("name");
  await api("POST", "/api/v1/jetty/vms", {
    name,
    cpus: Number(data.get("cpus")),
    memory: data.get("memory"),
    disk: data.get("disk"),
    image: data.get("image"),
    ssh_public_key: data.get("sshPublicKey").trim(),
  });
  await loadEntities();
  state.selectedVmName = name;
}

async function runVmAction(name, action) {
  try {
    await api("POST", `/api/v1/jetty/vms/${encodeURIComponent(name)}/action`, { action });
    await loadEntities();
  } catch (err) {
    alert(`Could not ${action} ${name}: ${err.message}`);
  }
  render();
}

async function handleFormSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const kind = state.editor.kind;
  try {
    if (kind === "rule") await submitRuleForm(form);
    else if (kind === "credential") await submitCredentialForm(form);
    else if (kind === "vm") await submitVmForm(form);
    state.editor = null;
    state.activeView = kind + "s";
  } catch (err) {
    state.editor.errors = err.fields || {};
    state.editor.banner = Object.keys(err.fields || {}).length ? null : err.message;
  }
  render();
}

function applyQuickAdd(key) {
  const item = state.quickAddCatalog.find((i) => i.key === key);
  if (!item) return;
  state.editor.values = { name: item.key, command: item.command, ttlSeconds: item.ttl_seconds };
  render();
}

function bind() {
  document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  document.querySelectorAll("[data-connection]").forEach((row) => row.addEventListener("click", () => selectConnection(row.dataset.connection)));
  document.querySelectorAll("[data-rule]").forEach((row) => row.addEventListener("click", (e) => { if (!e.target.closest("button")) selectRule(row.dataset.rule); }));
  document.querySelectorAll("[data-vm]").forEach((row) => row.addEventListener("click", (e) => { if (!e.target.closest("button")) selectVm(row.dataset.vm); }));
  document.querySelectorAll("[data-credential]").forEach((row) => row.addEventListener("click", (e) => { if (!e.target.closest("button")) selectCredential(row.dataset.credential); }));

  document.querySelector("[data-refresh-logs]")?.addEventListener("click", async () => { await loadConnections(); render(); });

  document.querySelectorAll("[data-new]").forEach((b) =>
    b.addEventListener("click", () => ({ rules: openRuleEditor, credentials: openCredentialEditor, vms: openVmEditor }[b.dataset.new]()))
  );
  document.querySelectorAll("[data-edit-rule]").forEach((b) => b.addEventListener("click", () => openRuleEditor(b.dataset.editRule)));
  document.querySelectorAll("[data-delete-rule]").forEach((b) => b.addEventListener("click", () => deleteEntity("rule", b.dataset.deleteRule)));
  document.querySelectorAll("[data-delete-vm]").forEach((b) => b.addEventListener("click", () => deleteEntity("vm", b.dataset.deleteVm)));
  document.querySelectorAll("[data-vm-action]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    runVmAction(b.dataset.vmAction, b.dataset.action);
  }));
  document.querySelectorAll("[data-delete-credential]").forEach((b) => b.addEventListener("click", () => deleteEntity("credential", b.dataset.deleteCredential)));
  document.querySelectorAll("[data-close-editor]").forEach((b) => b.addEventListener("click", closeEditor));
  document.querySelectorAll("[data-quick-add]").forEach((b) => b.addEventListener("click", () => applyQuickAdd(b.dataset.quickAdd)));

  const actionSelect = document.querySelector("#f-action");
  actionSelect?.addEventListener("change", () => {
    document.querySelector("#credential-fields").style.display = actionSelect.value === "allow_with_credential" ? "grid" : "none";
  });
  const injectionSelect = document.querySelector("#f-injection");
  injectionSelect?.addEventListener("change", () => {
    document.querySelector("#username-field").style.display = injectionSelect.value === "basic" ? "grid" : "none";
  });

  document.querySelector("#entity-form")?.addEventListener("submit", handleFormSubmit);
}

async function init() {
  try {
    await Promise.all([loadEntities(), loadConnections(), loadQuickAddCatalog()]);
    try { state.jettyStatus = await api("GET", "/api/v1/status"); } catch { state.jettyStatus = null; }
  } catch (err) {
    state.loadError = `Failed to load from the Management API: ${err.message}`;
  }
  render();
}

init();
