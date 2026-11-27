// Package ca generates and stores Jetty's Interception CA in state.sqlite.
package ca

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"database/sql"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"math/big"
	"os"
	"time"
)

const (
	SubjectName   = "Orca Local Interception CA"
	ValidityYears = 10
)

// Row mirrors the interception_ca table row.
type Row struct {
	CertificatePEM    string
	PrivateKeyPEM     string
	FingerprintSHA256 string
	NotBefore         string
	NotAfter          string
	CreatedAt         string
}

// Get returns the stored CA row, or nil if none exists.
func Get(db *sql.DB) (*Row, error) {
	var r Row
	err := db.QueryRow(
		`SELECT certificate_pem, private_key_pem, fingerprint_sha256, not_before, not_after, created_at
		 FROM interception_ca WHERE id = 1`,
	).Scan(&r.CertificatePEM, &r.PrivateKeyPEM, &r.FingerprintSHA256, &r.NotBefore, &r.NotAfter, &r.CreatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &r, nil
}

// EnsureGenerated returns the stored CA or creates a new 4096-bit RSA CA.
func EnsureGenerated(db *sql.DB) (*Row, error) {
	if existing, err := Get(db); err != nil || existing != nil {
		return existing, err
	}
	certPEM, keyPEM, fp, notBefore, notAfter, err := generate()
	if err != nil {
		return nil, err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err = db.Exec(
		`INSERT OR IGNORE INTO interception_ca
		 (id, certificate_pem, private_key_pem, fingerprint_sha256, not_before, not_after, created_at)
		 VALUES (1, ?, ?, ?, ?, ?, ?)`,
		certPEM, keyPEM, fp, notBefore.Format(time.RFC3339Nano), notAfter.Format(time.RFC3339Nano), now,
	)
	if err != nil {
		return nil, err
	}
	return Get(db)
}

func generate() (certPEM, keyPEM, fingerprint string, notBefore, notAfter time.Time, err error) {
	key, err := rsa.GenerateKey(rand.Reader, 4096)
	if err != nil {
		return "", "", "", time.Time{}, time.Time{}, err
	}
	serialLimit := new(big.Int).Lsh(big.NewInt(1), 128)
	serial, err := rand.Int(rand.Reader, serialLimit)
	if err != nil {
		return "", "", "", time.Time{}, time.Time{}, err
	}
	pubDER, err := x509.MarshalPKIXPublicKey(&key.PublicKey)
	if err != nil {
		return "", "", "", time.Time{}, time.Time{}, err
	}
	ski := sha256.Sum256(pubDER)

	notBefore = time.Now().UTC()
	notAfter = notBefore.AddDate(ValidityYears, 0, 0)
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: SubjectName},
		NotBefore:             notBefore,
		NotAfter:              notAfter,
		KeyUsage:              x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign | x509.KeyUsageCRLSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
		SubjectKeyId:          ski[:],
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return "", "", "", time.Time{}, time.Time{}, err
	}
	sum := sha256.Sum256(der)
	fingerprint = hex.EncodeToString(sum[:])

	certBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return "", "", "", time.Time{}, time.Time{}, err
	}
	keyBytes := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	return string(certBytes), string(keyBytes), fingerprint, notBefore, notAfter, nil
}

// Materialize atomically writes the combined cert+key PEM with mode 0600.
func Materialize(row *Row, path string) error {
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.WriteString(row.CertificatePEM + row.PrivateKeyPEM); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}
