package ca

import (
	"crypto/x509"
	"encoding/pem"
	"path/filepath"
	"testing"

	"github.com/cau777/jetty-vm/internal/db"
)

func TestEnsureGeneratedAndMaterialize(t *testing.T) {
	dir := t.TempDir()
	conn, err := db.Open(filepath.Join(dir, "state.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := db.MigrateState(conn); err != nil {
		t.Fatal(err)
	}
	row1, err := EnsureGenerated(conn)
	if err != nil {
		t.Fatal(err)
	}
	row2, err := EnsureGenerated(conn)
	if err != nil {
		t.Fatal(err)
	}
	if row1.FingerprintSHA256 != row2.FingerprintSHA256 {
		t.Fatal("CA was regenerated unexpectedly")
	}
	blk, _ := pem.Decode([]byte(row1.CertificatePEM))
	if blk == nil {
		t.Fatal("invalid cert PEM")
	}
	cert, err := x509.ParseCertificate(blk.Bytes)
	if err != nil || !cert.IsCA || len(cert.SubjectKeyId) == 0 {
		t.Fatalf("invalid CA cert: %v", err)
	}
	outPath := filepath.Join(dir, "mitmproxy-ca.pem")
	if err := Materialize(row1, outPath); err != nil {
		t.Fatal(err)
	}
}
