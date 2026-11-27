ALTER TABLE rules RENAME TO rules_legacy;

CREATE TABLE rules (
    scope TEXT NOT NULL,
    name TEXT NOT NULL,
    priority INTEGER NOT NULL,
    vm_selector_json TEXT NOT NULL,
    hostname TEXT NOT NULL,
    action_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (scope, name),
    UNIQUE (scope, priority),
    CHECK (
        (scope = '' AND json_extract(vm_selector_json, '$.type') = 'all')
        OR (scope != '' AND json_extract(vm_selector_json, '$.type') = 'only'
            AND json_array_length(vm_selector_json, '$.vms') = 1
            AND json_extract(vm_selector_json, '$.vms[0]') = scope)
    )
);

INSERT INTO rules
SELECT '', name, priority, vm_selector_json, hostname, action_json, created_at, updated_at
FROM rules_legacy WHERE json_extract(vm_selector_json, '$.type') = 'all';

INSERT INTO rules
SELECT vm.value, r.name, r.priority,
       json_object('type', 'only', 'vms', json_array(vm.value)),
       r.hostname, r.action_json, r.created_at, r.updated_at
FROM rules_legacy r, json_each(r.vm_selector_json, '$.vms') vm
WHERE json_extract(r.vm_selector_json, '$.type') = 'only';

DROP TABLE rules_legacy;
