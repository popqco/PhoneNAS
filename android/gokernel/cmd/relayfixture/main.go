// Command relayfixture serves only freshly generated local acceptance data.
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"phonenas.local/gokernel"
)

func main() {
	if err := run(); err != nil {
		// The Python owner captures only the generic error, never the pipe input.
		os.Stderr.WriteString("isolated DAV fixture failed\n")
		os.Exit(1)
	}
}

func run() error {
	root, err := os.MkdirTemp("", "PhoneNAS-relay-fixture-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(root)
	for _, subdir := range []string{"control", "shared", "readonly"} {
		if err := os.Mkdir(filepath.Join(root, subdir), 0700); err != nil {
			return err
		}
	}
	return serve(root)
}

func serve(root string) error {
	var auth struct{ Username, Password string }
	if err := json.NewDecoder(io.LimitReader(os.Stdin, 1024)).Decode(&auth); err != nil {
		return err
	}
	// Credentials are supplied only over the owning test's pipe. The roots and
	// listener are always new and cannot be pointed at a device or other host.
	dav, err := gokernel.New(gokernel.Config{
		Username: auth.Username, Password: auth.Password, ControlRoot: filepath.Join(root, "control"),
		Shares: []gokernel.Share{
			{Name: "dataset", Root: filepath.Join(root, "shared")},
			{Name: "readonly", Root: filepath.Join(root, "readonly"), ReadOnly: true},
		},
	})
	if err != nil {
		return err
	}
	defer dav.Close()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return err
	}
	now := time.Now()
	leaf := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "PhoneNAS isolated fixture"},
		NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IPAddresses: []net.IP{net.IPv4(127, 0, 0, 1)},
		BasicConstraintsValid: true}
	der, err := x509.CreateCertificate(rand.Reader, leaf, leaf, &key.PublicKey, key)
	if err != nil {
		return err
	}
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return err
	}
	defer listener.Close()
	server := &http.Server{Handler: dav, ReadHeaderTimeout: 3 * time.Second, ReadTimeout: 120 * time.Second, WriteTimeout: 120 * time.Second}
	report := map[string]string{"url": "https://" + listener.Addr().String(), "root": root,
		"pem": string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))}
	if err := json.NewEncoder(os.Stdout).Encode(report); err != nil {
		return err
	}
	go func() { _, _ = io.Copy(io.Discard, os.Stdin); _ = server.Close() }()
	err = server.Serve(tls.NewListener(listener, &tls.Config{MinVersion: tls.VersionTLS12,
		Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}}))
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}
