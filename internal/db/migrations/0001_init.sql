CREATE TABLE vms (
    name TEXT PRIMARY KEY,
    ip_address TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE credentials (
    name TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE rules (
    name TEXT PRIMARY KEY,
    priority INTEGER NOT NULL UNIQUE,
    vm_selector_json TEXT NOT NULL,
    hostname TEXT NOT NULL,
    action_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE interception_ca (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    certificate_pem TEXT NOT NULL,
    private_key_pem TEXT NOT NULL,
    fingerprint_sha256 TEXT NOT NULL,
    not_before TEXT NOT NULL,
    not_after TEXT NOT NULL,
    created_at TEXT NOT NULL
);
