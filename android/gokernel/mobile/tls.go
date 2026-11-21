package mobile

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"io"
	"math/big"
	"net"
	"net/netip"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

var errTLSDirectory = errors.New("TLS storage requires a private absolute directory outside all exports")
var errTLSIdentity = errors.New("TLS identity is unavailable or invalid; existing identity files are never replaced")

type tlsIdentity struct {
	certificate tls.Certificate
	publicPEM   string
	fingerprint string
}

func loadTLSIdentity(config configuration) (*tlsIdentity, error) {
	if err := os.Mkdir(config.TLSDirectory, 0700); err != nil && !os.IsExist(err) {
		return nil, errTLSDirectory
	}
	if err := validateTLSDirectory(config); err != nil {
		return nil, err
	}
	root, err := os.OpenRoot(config.TLSDirectory)
	if err != nil {
		return nil, errTLSDirectory
	}
	defer root.Close()
	certInfo, certErr := root.Lstat("server.crt")
	keyInfo, keyErr := root.Lstat("server.key")
	var certificatePEM, privatePEM []byte
	address, _ := netip.ParseAddrPort(config.RemoteListen) // validated before storage access
	if os.IsNotExist(certErr) && os.IsNotExist(keyErr) {
		certificatePEM, privatePEM, err = generateTLSIdentity(address.Addr())
		if err != nil {
			return nil, errTLSIdentity
		}
		// Exclusive creation never overwrites an existing or concurrently created
		// half-pair. A crash or failed write leaves a fail-closed identity.
		if err := writeIdentityFile(root, "server.key", privatePEM); err != nil {
			return nil, errTLSIdentity
		}
		if err := writeIdentityFile(root, "server.crt", certificatePEM); err != nil {
			return nil, errTLSIdentity
		}
	} else {
		if certErr != nil || keyErr != nil || !certInfo.Mode().IsRegular() || !keyInfo.Mode().IsRegular() ||
			(runtime.GOOS != "windows" && keyInfo.Mode().Perm() != 0600) {
			return nil, errTLSIdentity
		}
		certificatePEM, err = readIdentityFile(root, "server.crt")
		if err != nil {
			return nil, errTLSIdentity
		}
		privatePEM, err = readIdentityFile(root, "server.key")
		if err != nil {
			return nil, errTLSIdentity
		}
	}
	if !singleIdentityPEM(certificatePEM, "CERTIFICATE") ||
		(!singleIdentityPEM(privatePEM, "EC PRIVATE KEY") && !singleIdentityPEM(privatePEM, "PRIVATE KEY")) {
		return nil, errTLSIdentity
	}
	pair, err := tls.X509KeyPair(certificatePEM, privatePEM)
	if err != nil || len(pair.Certificate) != 1 {
		return nil, errTLSIdentity
	}
	certificate, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		return nil, errTLSIdentity
	}
	key, ok := pair.PrivateKey.(*ecdsa.PrivateKey)
	now := time.Now()
	if !ok || key.Curve != elliptic.P256() || certificate.IsCA || certificate.KeyUsage&x509.KeyUsageDigitalSignature == 0 ||
		len(certificate.ExtKeyUsage) != 1 || certificate.ExtKeyUsage[0] != x509.ExtKeyUsageServerAuth || len(certificate.UnknownExtKeyUsage) != 0 ||
		len(certificate.IPAddresses) != 1 || !certificate.IPAddresses[0].Equal(net.IP(address.Addr().AsSlice())) ||
		len(certificate.DNSNames) != 0 || len(certificate.URIs) != 0 || len(certificate.EmailAddresses) != 0 ||
		now.Before(certificate.NotBefore) || !now.Before(certificate.NotAfter) ||
		certificate.CheckSignature(certificate.SignatureAlgorithm, certificate.RawTBSCertificate, certificate.Signature) != nil {
		return nil, errTLSIdentity
	}
	pair.Leaf = certificate
	digest := sha256.Sum256(certificate.Raw)
	return &tlsIdentity{
		certificate: pair, publicPEM: string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certificate.Raw})),
		fingerprint: hex.EncodeToString(digest[:]),
	}, nil
}

func singleIdentityPEM(data []byte, kind string) bool {
	text := strings.TrimSpace(string(data))
	if !strings.HasPrefix(text, "-----BEGIN "+kind+"-----") {
		return false
	}
	block, rest := pem.Decode([]byte(text))
	return block != nil && block.Type == kind && len(block.Headers) == 0 && strings.TrimSpace(string(rest)) == ""
}

func generateTLSIdentity(address netip.Addr) ([]byte, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return nil, nil, err
	}
	serial.Add(serial, big.NewInt(1))
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber: serial, Subject: pkix.Name{CommonName: "PhoneNAS"},
		NotBefore: now.Add(-5 * time.Minute), NotAfter: now.AddDate(5, 0, 0),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses: []net.IP{net.IP(address.AsSlice())}, BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return nil, nil, err
	}
	privateDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: privateDER}), nil
}

func writeIdentityFile(root *os.Root, name string, data []byte) error {
	file, err := root.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err := file.Write(data); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	return file.Close()
}

func readIdentityFile(root *os.Root, name string) ([]byte, error) {
	file, err := root.Open(name)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, (64<<10)+1))
	if err != nil || len(data) > 64<<10 {
		return nil, errTLSIdentity
	}
	return data, nil
}

// The host supplies its filesDir/tls. On Android, never accept shared storage,
// another filesystem location, or an export that could publish the private key.
func validateTLSDirectory(config configuration) error {
	dir := config.TLSDirectory
	if !filepath.IsAbs(dir) || filepath.Clean(dir) != dir || strings.HasPrefix(filepath.ToSlash(dir), "//") {
		return errTLSDirectory
	}
	if runtime.GOOS == "android" && !androidPrivateFilesDirectory(dir) {
		return errTLSDirectory
	}
	roots := []string{config.ControlRoot}
	for _, share := range config.Shares {
		roots = append(roots, share.Root)
	}
	for _, root := range roots {
		if directoriesOverlap(dir, filepath.Clean(root)) {
			return errTLSDirectory
		}
	}
	for part := dir; ; part = filepath.Dir(part) {
		info, err := os.Lstat(part)
		if os.IsNotExist(err) && part == dir {
			// Only the final, private TLS directory may need creating.
			continue
		}
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return errTLSDirectory
		}
		if part == dir && runtime.GOOS != "windows" && info.Mode().Perm()&0077 != 0 {
			return errTLSDirectory
		}
		if filepath.Dir(part) == part {
			break
		}
	}
	return nil
}

func directoriesOverlap(a, b string) bool {
	if runtime.GOOS == "windows" {
		a, b = strings.ToLower(a), strings.ToLower(b)
	}
	sep := string(filepath.Separator)
	return a == b || strings.HasPrefix(a, strings.TrimSuffix(b, sep)+sep) || strings.HasPrefix(b, strings.TrimSuffix(a, sep)+sep)
}

func androidPrivateFilesDirectory(dir string) bool {
	parts := strings.Split(strings.TrimPrefix(filepath.ToSlash(dir), "/"), "/")
	if len(parts) >= 4 && parts[0] == "data" && parts[1] == "data" && parts[2] != "" && parts[3] == "files" {
		return true
	}
	if len(parts) < 5 || parts[0] != "data" || (parts[1] != "user" && parts[1] != "user_de") || parts[2] == "" || parts[3] == "" || parts[4] != "files" {
		return false
	}
	for _, digit := range parts[2] {
		if digit < '0' || digit > '9' {
			return false
		}
	}
	return true
}
