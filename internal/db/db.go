// Package db manages Jetty's SQLite connections, migrations, and repositories.
package db

import (
	"database/sql"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"sort"
	"strings"
	"time"

	_ "modernc.org/sqlite"
)

//go:embed migrations/*.sql requests_migrations/*.sql
var migrationsFS embed.FS

// NowISO returns an RFC3339 UTC timestamp string matching Python db.now_iso().
func NowISO() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}

// Open opens a SQLite database with WAL journal mode, foreign keys, and busy timeout.
func Open(path string) (*sql.DB, error) {
	conn, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	// SQLite serializes writes cleanly with a single pooled connection in Go.
	conn.SetMaxOpenConns(1)
	for _, pragma := range []string{
		"PRAGMA foreign_keys = ON;",
		"PRAGMA journal_mode = WAL;",
		"PRAGMA busy_timeout = 5000;",
	} {
		if _, err := conn.Exec(pragma); err != nil {
			conn.Close()
			return nil, err
		}
	}
	return conn, nil
}

// MigrateState applies all state.sqlite migrations idempotently.
func MigrateState(conn *sql.DB) error {
	return migrateDir(conn, "migrations")
}

// MigrateRequests applies all requests.sqlite migrations idempotently.
func MigrateRequests(conn *sql.DB) error {
	return migrateDir(conn, "requests_migrations")
}

func migrateDir(conn *sql.DB, dir string) error {
	if _, err := conn.Exec(`CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)`); err != nil {
		return err
	}
	entries, err := fs.ReadDir(migrationsFS, dir)
	if err != nil {
		return err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".sql") {
			continue
		}
		version := strings.TrimSuffix(entry.Name(), ".sql")
		var exists int
		err := conn.QueryRow(`SELECT 1 FROM schema_migrations WHERE version = ?`, version).Scan(&exists)
		if err == nil {
			continue
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		raw, err := fs.ReadFile(migrationsFS, dir+"/"+entry.Name())
		if err != nil {
			return err
		}
		tx, err := conn.Begin()
		if err != nil {
			return err
		}
		if _, err := tx.Exec(string(raw)); err != nil {
			tx.Rollback()
			return fmt.Errorf("migration %s: %w", entry.Name(), err)
		}
		if _, err := tx.Exec(`INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)`, version, NowISO()); err != nil {
			tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}

// --- VMs ---

type VMRow struct {
	Name      string
	IPAddress string
	CreatedAt string
	UpdatedAt string
}

func GetVM(conn *sql.DB, name string) (*VMRow, error) {
	var r VMRow
	err := conn.QueryRow(`SELECT name, ip_address, created_at, updated_at FROM vms WHERE name = ?`, name).
		Scan(&r.Name, &r.IPAddress, &r.CreatedAt, &r.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return &r, err
}

func ListVMs(conn *sql.DB) ([]VMRow, error) {
	rows, err := conn.Query(`SELECT name, ip_address, created_at, updated_at FROM vms ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []VMRow
	for rows.Next() {
		var r VMRow
		if err := rows.Scan(&r.Name, &r.IPAddress, &r.CreatedAt, &r.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func VMNames(conn *sql.DB) (map[string]struct{}, error) {
	rows, err := conn.Query(`SELECT name FROM vms`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]struct{})
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		out[name] = struct{}{}
	}
	return out, rows.Err()
}

func VMNameForIP(conn *sql.DB, ip string) (string, error) {
	if ip == "" {
		return "", nil
	}
	var name string
	err := conn.QueryRow(`SELECT name FROM vms WHERE ip_address = ?`, ip).Scan(&name)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return name, err
}

func IPInUseByOtherVM(conn *sql.DB, ip, excludeName string) (bool, error) {
	var exists int
	err := conn.QueryRow(`SELECT 1 FROM vms WHERE ip_address = ? AND name != ?`, ip, excludeName).Scan(&exists)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func PutVM(conn *sql.DB, name, ipAddress string) (*VMRow, bool, error) {
	existing, err := GetVM(conn, name)
	if err != nil {
		return nil, false, err
	}
	now := NowISO()
	if existing == nil {
		_, err = conn.Exec(
			`INSERT INTO vms (name, ip_address, created_at, updated_at) VALUES (?, ?, ?, ?)`,
			name, ipAddress, now, now,
		)
		if err != nil {
			return nil, false, err
		}
		row, err := GetVM(conn, name)
		return row, true, err
	}
	_, err = conn.Exec(`UPDATE vms SET ip_address = ?, updated_at = ? WHERE name = ?`, ipAddress, now, name)
	if err != nil {
		return nil, false, err
	}
	row, err := GetVM(conn, name)
	return row, false, err
}

func DeleteVM(conn *sql.DB, name string) (bool, error) {
	res, err := conn.Exec(`DELETE FROM vms WHERE name = ?`, name)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n > 0, nil
}

// --- Credentials ---

type CredentialRow struct {
	Name       string
	Command    string
	TTLSeconds int
	CreatedAt  string
	UpdatedAt  string
}

func GetCredential(conn *sql.DB, name string) (*CredentialRow, error) {
	var r CredentialRow
	err := conn.QueryRow(
		`SELECT name, command, ttl_seconds, created_at, updated_at FROM credentials WHERE name = ?`, name,
	).Scan(&r.Name, &r.Command, &r.TTLSeconds, &r.CreatedAt, &r.UpdatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return &r, err
}

func ListCredentials(conn *sql.DB) ([]CredentialRow, error) {
	rows, err := conn.Query(`SELECT name, command, ttl_seconds, created_at, updated_at FROM credentials ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []CredentialRow
	for rows.Next() {
		var r CredentialRow
		if err := rows.Scan(&r.Name, &r.Command, &r.TTLSeconds, &r.CreatedAt, &r.UpdatedAt); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func CredentialNames(conn *sql.DB) (map[string]struct{}, error) {
	rows, err := conn.Query(`SELECT name FROM credentials`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]struct{})
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		out[name] = struct{}{}
	}
	return out, rows.Err()
}

func PutCredential(conn *sql.DB, name, command string, ttlSeconds int) (*CredentialRow, bool, error) {
	existing, err := GetCredential(conn, name)
	if err != nil {
		return nil, false, err
	}
	now := NowISO()
	if existing == nil {
		_, err = conn.Exec(
			`INSERT INTO credentials (name, command, ttl_seconds, created_at, updated_at) VALUES (?, ?, ?, ?, ?)`,
			name, command, ttlSeconds, now, now,
		)
		if err != nil {
			return nil, false, err
		}
		row, err := GetCredential(conn, name)
		return row, true, err
	}
	_, err = conn.Exec(
		`UPDATE credentials SET command = ?, ttl_seconds = ?, updated_at = ? WHERE name = ?`,
		command, ttlSeconds, now, name,
	)
	if err != nil {
		return nil, false, err
	}
	row, err := GetCredential(conn, name)
	return row, false, err
}

func CredentialReferencedByRule(conn *sql.DB, name string) (bool, error) {
	var exists int
	err := conn.QueryRow(
		`SELECT 1 FROM rules WHERE json_extract(action_json, '$.type') = 'allow_with_credential' AND json_extract(action_json, '$.credential') = ?`,
		name,
	).Scan(&exists)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func DeleteCredential(conn *sql.DB, name string) error {
	_, err := conn.Exec(`DELETE FROM credentials WHERE name = ?`, name)
	return err
}

// --- Rules ---

type VMSelector struct {
	Type string   `json:"type"`
	VMs  []string `json:"vms,omitempty"`
}

type Injection struct {
	Type     string `json:"type"`
	Username string `json:"username,omitempty"`
	Name     string `json:"name,omitempty"`
	Service  string `json:"service,omitempty"`
	Region   string `json:"region,omitempty"`
}

type RuleAction struct {
	Type       string     `json:"type"`
	Credential string     `json:"credential,omitempty"`
	PathPrefix string     `json:"path_prefix,omitempty"`
	Injection  *Injection `json:"injection,omitempty"`
}

type Rule struct {
	Name       string     `json:"name"`
	Scope      string     `json:"scope"`
	Priority   int        `json:"priority"`
	VMSelector VMSelector `json:"vm_selector"`
	Hostname   string     `json:"hostname"`
	Action     RuleAction `json:"action"`
	CreatedAt  string     `json:"created_at"`
	UpdatedAt  string     `json:"updated_at"`
}

func scanRule(scanner interface{ Scan(dest ...any) error }) (*Rule, error) {
	var r Rule
	var selJSON, actJSON string
	if err := scanner.Scan(&r.Scope, &r.Name, &r.Priority, &selJSON, &r.Hostname, &actJSON, &r.CreatedAt, &r.UpdatedAt); err != nil {
		return nil, err
	}
	if err := json.Unmarshal([]byte(selJSON), &r.VMSelector); err != nil {
		return nil, err
	}
	if err := json.Unmarshal([]byte(actJSON), &r.Action); err != nil {
		return nil, err
	}
	return &r, nil
}

func GetRule(conn *sql.DB, name, scope string) (*Rule, error) {
	row := conn.QueryRow(
		`SELECT scope, name, priority, vm_selector_json, hostname, action_json, created_at, updated_at
		 FROM rules WHERE scope = ? AND name = ?`, scope, name,
	)
	r, err := scanRule(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return r, err
}

func ListRules(conn *sql.DB, scope *string) ([]Rule, error) {
	var rows *sql.Rows
	var err error
	if scope != nil {
		rows, err = conn.Query(
			`SELECT scope, name, priority, vm_selector_json, hostname, action_json, created_at, updated_at
			 FROM rules WHERE scope = ? ORDER BY priority`, *scope,
		)
	} else {
		rows, err = conn.Query(
			`SELECT scope, name, priority, vm_selector_json, hostname, action_json, created_at, updated_at
			 FROM rules ORDER BY scope, priority`,
		)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Rule
	for rows.Next() {
		r, err := scanRule(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *r)
	}
	return out, rows.Err()
}

func ListRulesNamed(conn *sql.DB, name string) ([]Rule, error) {
	rows, err := conn.Query(
		`SELECT scope, name, priority, vm_selector_json, hostname, action_json, created_at, updated_at
		 FROM rules WHERE name = ? ORDER BY scope`, name,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Rule
	for rows.Next() {
		r, err := scanRule(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *r)
	}
	return out, rows.Err()
}

func RulePriorityInUseByOther(conn *sql.DB, priority int, excludeName, scope string) (bool, error) {
	var exists int
	err := conn.QueryRow(
		`SELECT 1 FROM rules WHERE scope = ? AND priority = ? AND name != ?`, scope, priority, excludeName,
	).Scan(&exists)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func PutRule(conn *sql.DB, name string, priority int, sel VMSelector, hostname string, action RuleAction) (*Rule, bool, error) {
	scope := ""
	if sel.Type != "all" && len(sel.VMs) > 0 {
		scope = sel.VMs[0]
	}
	existing, err := GetRule(conn, name, scope)
	if err != nil {
		return nil, false, err
	}
	now := NowISO()
	selBytes, err := json.Marshal(sel)
	if err != nil {
		return nil, false, err
	}
	actBytes, err := json.Marshal(action)
	if err != nil {
		return nil, false, err
	}
	if existing == nil {
		_, err = conn.Exec(
			`INSERT INTO rules (scope, name, priority, vm_selector_json, hostname, action_json, created_at, updated_at)
			 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
			scope, name, priority, string(selBytes), hostname, string(actBytes), now, now,
		)
		if err != nil {
			return nil, false, err
		}
		r, err := GetRule(conn, name, scope)
		return r, true, err
	}
	_, err = conn.Exec(
		`UPDATE rules SET priority = ?, vm_selector_json = ?, hostname = ?, action_json = ?, updated_at = ?
		 WHERE scope = ? AND name = ?`,
		priority, string(selBytes), hostname, string(actBytes), now, scope, name,
	)
	if err != nil {
		return nil, false, err
	}
	r, err := GetRule(conn, name, scope)
	return r, false, err
}

func DeleteRule(conn *sql.DB, name, scope string) error {
	_, err := conn.Exec(`DELETE FROM rules WHERE scope = ? AND name = ?`, scope, name)
	return err
}

// --- Port Forwards & Reverse Forwards ---

type PortForwardRow struct {
	HostPort  int
	VMName    string
	VMPort    int
	CreatedAt string
}

func ListPortForwards(conn *sql.DB) ([]PortForwardRow, error) {
	rows, err := conn.Query(`SELECT host_port, vm_name, vm_port, created_at FROM port_forwards ORDER BY host_port`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []PortForwardRow
	for rows.Next() {
		var r PortForwardRow
		if err := rows.Scan(&r.HostPort, &r.VMName, &r.VMPort, &r.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func PutPortForward(conn *sql.DB, hostPort int, vmName string, vmPort int) error {
	_, err := conn.Exec(
		`INSERT INTO port_forwards (host_port, vm_name, vm_port, created_at)
		 VALUES (?, ?, ?, ?)
		 ON CONFLICT(host_port) DO UPDATE SET vm_name = excluded.vm_name, vm_port = excluded.vm_port`,
		hostPort, vmName, vmPort, NowISO(),
	)
	return err
}

func DeletePortForward(conn *sql.DB, hostPort int) error {
	_, err := conn.Exec(`DELETE FROM port_forwards WHERE host_port = ?`, hostPort)
	return err
}

type ReverseForwardRow struct {
	VMName    string
	VMPort    int
	HostPort  int
	CreatedAt string
}

func ListReverseForwards(conn *sql.DB) ([]ReverseForwardRow, error) {
	rows, err := conn.Query(`SELECT vm_name, vm_port, host_port, created_at FROM reverse_forwards ORDER BY vm_name, vm_port`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []ReverseForwardRow
	for rows.Next() {
		var r ReverseForwardRow
		if err := rows.Scan(&r.VMName, &r.VMPort, &r.HostPort, &r.CreatedAt); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func PutReverseForward(conn *sql.DB, vmName string, vmPort, hostPort int) error {
	_, err := conn.Exec(
		`INSERT INTO reverse_forwards (vm_name, vm_port, host_port, created_at)
		 VALUES (?, ?, ?, ?)
		 ON CONFLICT(vm_name, vm_port) DO UPDATE SET host_port = excluded.host_port`,
		vmName, vmPort, hostPort, NowISO(),
	)
	return err
}

func DeleteReverseForward(conn *sql.DB, vmName string, vmPort int) error {
	_, err := conn.Exec(`DELETE FROM reverse_forwards WHERE vm_name = ? AND vm_port = ?`, vmName, vmPort)
	return err
}
