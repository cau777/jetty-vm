CREATE TABLE connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    vm_name TEXT NOT NULL,
    destination_ip TEXT NOT NULL,
    destination_port INTEGER NOT NULL,
    destination_hostname TEXT,
    sni_present INTEGER NOT NULL,
    ech_present INTEGER NOT NULL,
    duration_ms INTEGER,
    intercepted INTEGER NOT NULL,
    outcome TEXT,
    matched_rule_json TEXT,
    intercepted_by_rule_json TEXT
);

CREATE INDEX idx_connections_vm_name ON connections(vm_name);
CREATE INDEX idx_connections_destination_hostname ON connections(destination_hostname);
CREATE INDEX idx_connections_outcome ON connections(outcome);
CREATE INDEX idx_connections_started_at ON connections(started_at);

CREATE TABLE http_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id INTEGER NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    occurred_at TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    query_keys_json TEXT NOT NULL,
    status INTEGER,
    status_origin TEXT,
    latency_ms INTEGER,
    outcome TEXT NOT NULL,
    matched_rule_json TEXT,
    matched_credential TEXT,
    trace_json TEXT NOT NULL,
    headers_json TEXT NOT NULL
);

CREATE INDEX idx_http_requests_connection_id ON http_requests(connection_id);
CREATE INDEX idx_http_requests_outcome ON http_requests(outcome);
CREATE INDEX idx_http_requests_status ON http_requests(status);
