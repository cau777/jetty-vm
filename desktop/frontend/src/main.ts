import "./style.css";
import type { ConnectionLog, Credential, QuickAddEntry, RequestDetail, Rule, Status, Vm } from "./types";

type View = "logs" | "rules" | "credentials" | "ca" | "vm";

const state: {
  view: View;
  selectedVm: string;
  status: Status | null;
  vms: Vm[];
  rules: Rule[];
  credentials: Credential[];
  catalog: QuickAddEntry[];
  logs: ConnectionLog[];
  selectedLog: RequestDetail | null;
  ca: { subject: string; fingerprint_sha256: string; not_before: string; not_after: string; certificate_pem: string } | null;
  ports: Array<{ vm_name: string; vm_port: number; host_port: number; persistent: boolean; state: string; url: string; error?: string }>;
  reversePorts: Array<{ vm_name: string; vm_port: number; host_port: number; persistent: boolean; state: string; error?: string }>;
  filterVm: string;
  filterDecision: string;
  filterHost: string;
  error: string;
  jobLog: string;
  showRuleEditor: boolean;
  showCredEditor: boolean;
  showVmCreator: boolean;
} = {
  view: "logs",
  selectedVm: "",
  status: null,
  vms: [],
  rules: [],
  credentials: [],
  catalog: [],
  logs: [],
  selectedLog: null,
  ca: null,
  ports: [],
  reversePorts: [],
  filterVm: "",
  filterDecision: "",
  filterHost: "",
  error: "",
  jobLog: "",
  showRuleEditor: false,
  showCredEditor: false,
  showVmCreator: false,
};

async function rpc<T>(method: string, ...args: unknown[]): Promise<T> {
  const fn = window.go?.main?.App?.[method];
  if (!fn) {
    throw new Error(`Wails binding App.${method} unavailable`);
  }
  return (await fn(...args)) as T;
}

function esc(v: unknown): string {
  return String(v ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function refreshAll() {
  try {
    state.error = "";
    const [status, vmsRes, rulesRes, credsRes, catRes, logsRes, caRes, portsRes, revRes] = await Promise.all([
      rpc<Status>("GetStatus"),
      rpc<{ vms: Vm[] }>("ListVms"),
      rpc<{ rules: Rule[] }>("ListRules", state.view === "vm" ? state.selectedVm : "", state.view === "rules"),
      rpc<{ credentials: Credential[] }>("ListCredentials"),
      rpc<{ entries: QuickAddEntry[] }>("GetQuickAddCatalog"),
      rpc<{ connections: ConnectionLog[] }>("ListRequests", state.filterVm, state.filterDecision, state.filterHost, 100),
      rpc<typeof state.ca>("GetCa"),
      rpc<{ ports: typeof state.ports }>("ListPorts", ""),
      rpc<{ ports: typeof state.reversePorts }>("ListReversePorts", ""),
    ]);
    state.status = status;
    state.vms = vmsRes.vms ?? [];
    state.rules = rulesRes.rules ?? [];
    state.credentials = credsRes.credentials ?? [];
    state.catalog = catRes.entries ?? [];
    state.logs = logsRes.connections ?? [];
    state.ca = caRes;
    state.ports = portsRes.ports ?? [];
    state.reversePorts = revRes.ports ?? [];
  } catch (err) {
    state.error = err instanceof Error ? err.message : String(err);
  }
  render();
}

function renderTopBar(): string {
  const st = state.status;
  const gwState = st?.gateway ?? "unknown";
  const agentState = st?.gateway_agent_service ?? "unknown";
  const dotCls = st?.state === "running" ? "ok" : st?.state === "failed" ? "err" : "";
  return `
    <div class="topbar">
      <div class="brand">JETTY</div>
      <div class="pill"><span class="dot ${dotCls}"></span>state: ${esc(st?.state ?? "connecting")}</div>
      <div class="pill">gateway: ${esc(gwState)}</div>
      <div class="pill">agent: ${esc(agentState)} (v${esc(st?.gateway_agent_version ?? "-")})</div>
      <div class="spacer"></div>
      <button class="btn primary" id="btn-gw-setup">Gateway Setup</button>
      <button class="btn" id="btn-refresh">Refresh</button>
    </div>`;
}

function renderSidebar(): string {
  const nav = [
    { key: "logs", label: "Logs" },
    { key: "rules", label: "Global Rules" },
    { key: "credentials", label: "Credentials" },
    { key: "ca", label: "Interception CA" },
  ];
  return `
    <div class="sidebar">
      <div class="nav-label">MANAGEMENT</div>
      ${nav
        .map(
          (n) =>
            `<button class="nav-btn ${state.view === n.key ? "active" : ""}" data-nav="${n.key}">${esc(n.label)}</button>`
        )
        .join("")}
      <div class="nav-label" style="margin-top:10px">ENVIRONMENTS</div>
      <button class="btn primary" id="btn-new-vm">+ New VM</button>
      ${state.vms
        .map(
          (v) =>
            `<button class="nav-btn ${state.view === "vm" && state.selectedVm === v.name ? "active" : ""}" data-vm="${esc(v.name)}">
              <span>${esc(v.name)}</span>
              <span class="tag">${esc(v.ip_address || v.status)}</span>
            </button>`
        )
        .join("")}
    </div>`;
}

function renderLogsPane(): string {
  return `
    <div class="header-row">
      <h2>Connection & Request Logs</h2>
    </div>
    <div class="filters">
      <input id="f-vm" placeholder="Filter VM..." value="${esc(state.filterVm)}" />
      <input id="f-host" placeholder="Filter host..." value="${esc(state.filterHost)}" />
      <select id="f-dec">
        <option value="">All decisions</option>
        ${["allow_default", "allow_rule", "allow_credential", "block_rule", "block_unknown_vm"]
          .map((d) => `<option value="${d}" ${state.filterDecision === d ? "selected" : ""}>${d}</option>`)
          .join("")}
      </select>
      <button class="btn" id="btn-apply-filter">Apply</button>
    </div>
    <table>
      <thead><tr><th>ID</th><th>VM</th><th>Destination</th><th>Decision</th><th>Time</th></tr></thead>
      <tbody>
        ${state.logs
          .map((c) => {
            const label = c.intercepted ? "intercepted" : c.outcome || "unknown";
            const cls = c.outcome?.startsWith("block_") ? "block" : c.intercepted ? "intercept" : "allow";
            const sel = state.selectedLog?.connection?.id === c.id ? "selected" : "";
            return `<tr class="clickable ${sel}" data-log-id="${c.id}">
              <td>#${c.id}</td>
              <td>${esc(c.vm_name)}</td>
              <td>${esc(c.destination_hostname || c.destination_ip)}:${c.destination_port}</td>
              <td><span class="tag ${cls}">${esc(label)}</span></td>
              <td>${esc(c.started_at.replace("T", " ").slice(0, 19))}</td>
            </tr>`;
          })
          .join("")}
      </tbody>
    </table>`;
}

function renderRulesPane(scope: string): string {
  const title = scope ? `VM Rules: ${scope}` : "Global Rules";
  return `
    <div class="header-row">
      <h2>${esc(title)}</h2>
      <button class="btn primary" id="btn-new-rule">+ Add Rule</button>
    </div>
    ${state.showRuleEditor ? renderRuleForm(scope) : ""}
    <table>
      <thead><tr><th>Priority</th><th>Name</th><th>Hostname</th><th>Action</th><th></th></tr></thead>
      <tbody>
        ${state.rules
          .map(
            (r) => `<tr>
              <td>${r.priority}</td>
              <td>${esc(r.name)}</td>
              <td><code>${esc(r.hostname)}</code></td>
              <td>${esc(r.action?.type)} ${r.action?.credential ? `(${esc(r.action.credential)} ${esc(r.action.path_prefix)})` : ""}</td>
              <td><button class="btn danger" data-del-rule="${esc(r.name)}" data-del-scope="${esc(r.scope)}">Delete</button></td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table>`;
}

function renderRuleForm(scope: string): string {
  return `
    <div class="card">
      <h3>New / Update Rule (${scope ? esc(scope) : "global"})</h3>
      <div class="filters">
        <input id="r-name" placeholder="rule-name" />
        <input id="r-prio" type="number" value="10" style="width:90px" />
        <input id="r-host" placeholder="api.github.com or @aws" />
        <select id="r-action">
          <option value="allow_with_credential">allow_with_credential</option>
          <option value="allow">allow</option>
          <option value="block">block</option>
        </select>
      </div>
      <div class="filters">
        <select id="r-cred">
          ${state.credentials.map((c) => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("")}
        </select>
        <input id="r-prefix" value="/" placeholder="/path/prefix" />
        <select id="r-inj">
          <option value="bearer">bearer</option>
          <option value="basic">basic</option>
          <option value="header">header</option>
          <option value="aws_sigv4">aws_sigv4</option>
        </select>
        <input id="r-extra" placeholder="username / header / auto" />
        <button class="btn primary" id="btn-save-rule">Save Rule</button>
        <button class="btn" id="btn-cancel-rule">Cancel</button>
      </div>
    </div>`;
}

function renderCredentialsPane(): string {
  return `
    <div class="header-row">
      <h2>Credentials</h2>
      <button class="btn primary" id="btn-new-cred">+ Add Credential</button>
    </div>
    ${
      state.showCredEditor
        ? `<div class="card">
            <h3>Add / Update Credential</h3>
            <div class="form-row"><label>Name</label><input id="c-name" placeholder="github-host-login" /></div>
            <div class="form-row"><label>TTL (seconds)</label><input id="c-ttl" type="number" value="300" /></div>
            <div class="form-row"><label>Bash Command</label><textarea id="c-cmd" placeholder="gh auth token"></textarea></div>
            <button class="btn primary" id="btn-save-cred">Save</button>
            <button class="btn" id="btn-cancel-cred">Cancel</button>
          </div>`
        : ""
    }
    <table>
      <thead><tr><th>Name</th><th>Status</th><th>TTL</th><th>Actions</th></tr></thead>
      <tbody>
        ${state.credentials
          .map(
            (c) => `<tr>
              <td>${esc(c.name)}</td>
              <td><span class="tag ${c.status === "valid" ? "allow" : c.status === "error" ? "block" : ""}">${esc(c.status)}</span></td>
              <td>${c.ttl_seconds}s</td>
              <td>
                <button class="btn" data-refresh-cred="${esc(c.name)}">Refresh</button>
                <button class="btn danger" data-del-cred="${esc(c.name)}">Delete</button>
              </td>
            </tr>`
          )
          .join("")}
      </tbody>
    </table>
    <h3>Quick Add Catalog</h3>
    <div class="filters">
      ${state.catalog
        .map(
          (e) =>
            `<button class="btn" data-quick-add="${esc(e.key)}" title="${esc(e.notes)}">+ ${esc(e.display_name)}</button>`
        )
        .join("")}
    </div>`;
}

function renderCaPane(): string {
  const ca = state.ca;
  if (!ca) return "<p>Loading CA...</p>";
  return `
    <h2>Interception CA</h2>
    <div class="card">
      <div><strong>Subject:</strong> ${esc(ca.subject)}</div>
      <div><strong>SHA-256 Fingerprint:</strong> <code>${esc(ca.fingerprint_sha256)}</code></div>
      <div><strong>Valid:</strong> ${esc(ca.not_before)} → ${esc(ca.not_after)}</div>
    </div>
    <pre>${esc(ca.certificate_pem)}</pre>`;
}

function renderVmPane(): string {
  const vm = state.vms.find((v) => v.name === state.selectedVm);
  if (!vm) return "<p>Select a VM from the sidebar.</p>";
  const vmPorts = state.ports.filter((p) => p.vm_name === vm.name);
  const vmRev = state.reversePorts.filter((p) => p.vm_name === vm.name);
  return `
    <div class="header-row">
      <h2>${esc(vm.name)} <span class="tag">${esc(vm.ip_address || "no IP")}</span> <span class="tag">${esc(vm.status)}</span></h2>
      <div>
        <button class="btn" data-vm-act="start">Start</button>
        <button class="btn" data-vm-act="stop">Stop</button>
        <button class="btn" data-vm-act="restart">Restart</button>
        <button class="btn primary" data-handoff="claude">Handoff Claude</button>
        <button class="btn primary" data-handoff="codex">Handoff Codex</button>
        <button class="btn danger" id="btn-del-vm">Delete VM</button>
      </div>
    </div>
    <div class="card">
      <h3>Port Forwards (Host → VM)</h3>
      <div class="filters">
        <input id="pf-vm" type="number" placeholder="VM port" style="width:100px" />
        <input id="pf-host" type="number" placeholder="Host port" style="width:100px" />
        <label><input id="pf-pers" type="checkbox" /> Persistent</label>
        <button class="btn primary" id="btn-open-port">Open Port</button>
      </div>
      ${vmPorts
        .map(
          (p) =>
            `<div>${esc(p.url)} → ${p.vm_port} (${esc(p.state)}) <button class="btn danger" data-close-port="${p.host_port}">Close</button></div>`
        )
        .join("")}
      <h3>Reverse Forwards (VM → Host)</h3>
      <div class="filters">
        <input id="rf-host" type="number" placeholder="Host port" style="width:100px" />
        <input id="rf-vm" type="number" placeholder="VM port" style="width:100px" />
        <label><input id="rf-pers" type="checkbox" /> Persistent</label>
        <button class="btn primary" id="btn-open-rev">Open Reverse</button>
      </div>
      ${vmRev
        .map(
          (p) =>
            `<div>localhost:${p.vm_port} → host:${p.host_port} (${esc(p.state)}) <button class="btn danger" data-close-rev="${p.vm_port}">Close</button></div>`
        )
        .join("")}
    </div>
    ${renderRulesPane(vm.name)}`;
}

function renderVmCreator(): string {
  return `
    <div class="card">
      <h3>Create New Agent VM</h3>
      <div class="filters">
        <input id="vm-name" placeholder="vm-name" />
        <input id="vm-cpus" type="number" value="4" style="width:80px" />
        <input id="vm-mem" value="8GiB" style="width:90px" />
        <input id="vm-disk" value="40GiB" style="width:90px" />
        <button class="btn primary" id="btn-create-vm">Create</button>
        <button class="btn" id="btn-cancel-vm">Cancel</button>
      </div>
    </div>`;
}

function renderInspector(): string {
  if (!state.selectedLog) return "";
  const d = state.selectedLog;
  return `
    <div class="inspector">
      <div class="header-row">
        <h2>Connection #${d.connection.id}</h2>
        <button class="btn" id="btn-close-insp">Close</button>
      </div>
      <div><strong>VM:</strong> ${esc(d.connection.vm_name)}</div>
      <div><strong>Dest:</strong> ${esc(d.connection.destination_hostname || d.connection.destination_ip)}:${d.connection.destination_port}</div>
      <div><strong>Outcome:</strong> ${esc(d.connection.outcome || (d.connection.intercepted ? "intercepted" : ""))}</div>
      ${(d.http_requests ?? [])
        .map(
          (r) => `<div class="card" style="margin-top:10px">
            <div><strong>${esc(r.method)} ${esc(r.path)}</strong> → ${r.status || "-"} (${esc(r.outcome)})</div>
            ${r.matched_credential ? `<div>Credential: <code>${esc(r.matched_credential)}</code></div>` : ""}
            <h3>Headers (Redacted)</h3>
            <pre>${esc((r.headers ?? []).map((h) => `${h.name}: ${h.value}`).join("\n"))}</pre>
            <h3>Rule Trace</h3>
            <pre>${esc((r.trace ?? []).map((t) => `[${t.scope || "global"}:${t.priority}] ${t.rule_name} (${t.action_type}) -> ${t.result}`).join("\n"))}</pre>
          </div>`
        )
        .join("")}
    </div>`;
}

function render() {
  const root = document.getElementById("app")!;
  let pane = "";
  if (state.view === "logs") pane = renderLogsPane();
  else if (state.view === "rules") pane = renderRulesPane("");
  else if (state.view === "credentials") pane = renderCredentialsPane();
  else if (state.view === "ca") pane = renderCaPane();
  else pane = renderVmPane();

  root.innerHTML = `
    <div class="shell">
      ${renderTopBar()}
      <div class="main">
        ${renderSidebar()}
        <div class="content">
          <div class="pane">
            ${state.error ? `<div class="error-banner">${esc(state.error)}</div>` : ""}
            ${state.jobLog ? `<div class="card"><strong>Job:</strong> ${esc(state.jobLog)}</div>` : ""}
            ${state.showVmCreator ? renderVmCreator() : ""}
            ${pane}
          </div>
          ${state.view === "logs" ? renderInspector() : ""}
        </div>
      </div>
    </div>`;
  wireEvents();
}

function wireEvents() {
  document.getElementById("btn-refresh")?.addEventListener("click", refreshAll);
  document.getElementById("btn-gw-setup")?.addEventListener("click", async () => {
    try {
      await rpc("SetupGateway", "");
      await refreshAll();
    } catch (e) {
      state.error = String(e);
      render();
    }
  });
  document.getElementById("btn-new-vm")?.addEventListener("click", () => {
    state.showVmCreator = true;
    render();
  });
  document.getElementById("btn-cancel-vm")?.addEventListener("click", () => {
    state.showVmCreator = false;
    render();
  });
  document.getElementById("btn-create-vm")?.addEventListener("click", async () => {
    const name = (document.getElementById("vm-name") as HTMLInputElement).value.trim();
    const cpus = Number((document.getElementById("vm-cpus") as HTMLInputElement).value);
    const mem = (document.getElementById("vm-mem") as HTMLInputElement).value.trim();
    const disk = (document.getElementById("vm-disk") as HTMLInputElement).value.trim();
    state.showVmCreator = false;
    try {
      await rpc("CreateVm", name, cpus, mem, disk, "ubuntu:24.04", "virtual-machine", "");
      await refreshAll();
    } catch (e) {
      state.error = String(e);
      render();
    }
  });
  document.querySelectorAll<HTMLElement>("[data-nav]").forEach((el) =>
    el.addEventListener("click", () => {
      state.view = el.dataset.nav as View;
      refreshAll();
    })
  );
  document.querySelectorAll<HTMLElement>("[data-vm]").forEach((el) =>
    el.addEventListener("click", () => {
      state.view = "vm";
      state.selectedVm = el.dataset.vm || "";
      refreshAll();
    })
  );
  document.getElementById("btn-apply-filter")?.addEventListener("click", () => {
    state.filterVm = (document.getElementById("f-vm") as HTMLInputElement).value.trim();
    state.filterHost = (document.getElementById("f-host") as HTMLInputElement).value.trim();
    state.filterDecision = (document.getElementById("f-dec") as HTMLSelectElement).value;
    refreshAll();
  });
  document.querySelectorAll<HTMLElement>("[data-log-id]").forEach((el) =>
    el.addEventListener("click", async () => {
      state.selectedLog = await rpc<RequestDetail>("GetRequest", Number(el.dataset.logId));
      render();
    })
  );
  document.getElementById("btn-close-insp")?.addEventListener("click", () => {
    state.selectedLog = null;
    render();
  });
  document.getElementById("btn-new-rule")?.addEventListener("click", () => {
    state.showRuleEditor = true;
    render();
  });
  document.getElementById("btn-cancel-rule")?.addEventListener("click", () => {
    state.showRuleEditor = false;
    render();
  });
  document.getElementById("btn-save-rule")?.addEventListener("click", async () => {
    const scope = state.view === "vm" ? state.selectedVm : "";
    const injType = (document.getElementById("r-inj") as HTMLSelectElement).value;
    const extra = (document.getElementById("r-extra") as HTMLInputElement).value.trim();
    await rpc("PutRule", {
      name: (document.getElementById("r-name") as HTMLInputElement).value.trim(),
      priority: Number((document.getElementById("r-prio") as HTMLInputElement).value),
      scope,
      hostname: (document.getElementById("r-host") as HTMLInputElement).value.trim(),
      action_type: (document.getElementById("r-action") as HTMLSelectElement).value,
      credential: (document.getElementById("r-cred") as HTMLSelectElement)?.value || "",
      path_prefix: (document.getElementById("r-prefix") as HTMLInputElement).value.trim() || "/",
      injection_type: injType,
      username: injType === "basic" ? extra : "",
      header_name: injType === "header" ? extra : "",
      service: injType === "aws_sigv4" ? extra || "auto" : "",
      region: injType === "aws_sigv4" ? "auto" : "",
    });
    state.showRuleEditor = false;
    await refreshAll();
  });
  document.querySelectorAll<HTMLElement>("[data-del-rule]").forEach((el) =>
    el.addEventListener("click", async () => {
      await rpc("DeleteRule", el.dataset.delRule!, el.dataset.delScope || "");
      await refreshAll();
    })
  );
  document.getElementById("btn-new-cred")?.addEventListener("click", () => {
    state.showCredEditor = true;
    render();
  });
  document.getElementById("btn-cancel-cred")?.addEventListener("click", () => {
    state.showCredEditor = false;
    render();
  });
  document.getElementById("btn-save-cred")?.addEventListener("click", async () => {
    await rpc(
      "PutCredential",
      (document.getElementById("c-name") as HTMLInputElement).value.trim(),
      (document.getElementById("c-cmd") as HTMLTextAreaElement).value,
      Number((document.getElementById("c-ttl") as HTMLInputElement).value)
    );
    state.showCredEditor = false;
    await refreshAll();
  });
  document.querySelectorAll<HTMLElement>("[data-refresh-cred]").forEach((el) =>
    el.addEventListener("click", async () => {
      await rpc("RefreshCredential", el.dataset.refreshCred!);
      await refreshAll();
    })
  );
  document.querySelectorAll<HTMLElement>("[data-del-cred]").forEach((el) =>
    el.addEventListener("click", async () => {
      await rpc("DeleteCredential", el.dataset.delCred!);
      await refreshAll();
    })
  );
  document.querySelectorAll<HTMLElement>("[data-quick-add]").forEach((el) =>
    el.addEventListener("click", () => {
      const entry = state.catalog.find((c) => c.key === el.dataset.quickAdd);
      if (!entry) return;
      state.showCredEditor = true;
      render();
      (document.getElementById("c-name") as HTMLInputElement).value = entry.key;
      (document.getElementById("c-ttl") as HTMLInputElement).value = String(entry.ttl_seconds);
      (document.getElementById("c-cmd") as HTMLTextAreaElement).value = entry.command;
    })
  );
  document.querySelectorAll<HTMLElement>("[data-vm-act]").forEach((el) =>
    el.addEventListener("click", async () => {
      await rpc("VmAction", state.selectedVm, el.dataset.vmAct!);
      await refreshAll();
    })
  );
  document.querySelectorAll<HTMLElement>("[data-handoff]").forEach((el) =>
    el.addEventListener("click", async () => {
      await rpc("HandoffVm", state.selectedVm, el.dataset.handoff!);
    })
  );
  document.getElementById("btn-del-vm")?.addEventListener("click", async () => {
    await rpc("DeleteVm", state.selectedVm);
    state.view = "logs";
    state.selectedVm = "";
    await refreshAll();
  });
  document.getElementById("btn-open-port")?.addEventListener("click", async () => {
    const vp = Number((document.getElementById("pf-vm") as HTMLInputElement).value);
    const hp = Number((document.getElementById("pf-host") as HTMLInputElement).value || "0");
    const pers = (document.getElementById("pf-pers") as HTMLInputElement).checked;
    await rpc("OpenPort", state.selectedVm, vp, hp, pers);
    await refreshAll();
  });
  document.querySelectorAll<HTMLElement>("[data-close-port]").forEach((el) =>
    el.addEventListener("click", async () => {
      await rpc("ClosePort", state.selectedVm, Number(el.dataset.closePort));
      await refreshAll();
    })
  );
  document.getElementById("btn-open-rev")?.addEventListener("click", async () => {
    const hp = Number((document.getElementById("rf-host") as HTMLInputElement).value);
    const vp = Number((document.getElementById("rf-vm") as HTMLInputElement).value || "0");
    const pers = (document.getElementById("rf-pers") as HTMLInputElement).checked;
    await rpc("OpenReversePort", state.selectedVm, hp, vp, pers);
    await refreshAll();
  });
  document.querySelectorAll<HTMLElement>("[data-close-rev]").forEach((el) =>
    el.addEventListener("click", async () => {
      await rpc("CloseReversePort", state.selectedVm, Number(el.dataset.closeRev));
      await refreshAll();
    })
  );
}

window.runtime?.EventsOn("jetty:event", () => {
  void refreshAll();
});
window.runtime?.EventsOn("jetty:job", (ev: unknown) => {
  const job = ev as { kind?: string; target?: string; state?: string; current_step?: string; error_message?: string };
  state.jobLog = `${job.kind ?? "job"} (${job.target ?? ""}): ${job.state ?? ""} [${job.current_step ?? ""}] ${job.error_message ?? ""}`;
  render();
});

render();
void refreshAll();
