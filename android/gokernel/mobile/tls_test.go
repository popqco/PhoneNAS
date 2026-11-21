package mobile

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

const remoteTestPassword = "synthetic-remote-contract-password-32"

func remoteTestConfig(t *testing.T) map[string]any {
	t.Helper()
	base := t.TempDir()
	control, shared := filepath.Join(base, "control"), filepath.Join(base, "shared")
	for _, dir := range []string{control, shared} {
		if err := os.Mkdir(dir, 0700); err != nil {
			t.Fatal(err)
		}
	}
	return map[string]any{
		"listen": "127.0.0.1:0", "username": "remote-contract", "password": remoteTestPassword,
		"control_root": control, "shares": []map[string]any{{"name": "dataset", "root": shared}},
	}
}

func encodeRemoteConfig(t *testing.T, config map[string]any) string {
	t.Helper()
	data, err := json.Marshal(config)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

type remoteLoopback struct {
	listener    net.Listener
	trustedPeer bool
}

type tailscaleTestListener struct{ net.Listener }
type tailscaleTestConnection struct{ net.Conn }

func (l tailscaleTestListener) Accept() (net.Conn, error) {
	connection, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	return tailscaleTestConnection{connection}, nil
}

func (c tailscaleTestConnection) RemoteAddr() net.Addr {
	return &net.TCPAddr{IP: net.IPv4(100, 64, 1, 2), Port: 54321}
}

// Only this test adapter binds loopback in place of a validated Tailscale IP.
// TLS verification is never bypassed; trustedPeer changes only the test socket's
// RemoteAddr to exercise authenticated remote requests through the real filter.
func (l *remoteLoopback) listen(network, address string) (net.Listener, error) {
	if strings.HasPrefix(address, "100.") {
		listener, err := net.Listen("tcp4", "127.0.0.1:0")
		if err == nil {
			l.listener = listener
			if l.trustedPeer {
				return tailscaleTestListener{listener}, nil
			}
		}
		return listener, err
	}
	return net.Listen(network, address)
}

func (l *remoteLoopback) client(t *testing.T, server *Server, pin string) *http.Client {
	t.Helper()
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM([]byte(server.Certificate())) {
		t.Fatal("Certificate did not return a public PEM certificate")
	}
	transport := &http.Transport{
		TLSClientConfig: &tls.Config{
			RootCAs: pool, MinVersion: tls.VersionTLS12,
			VerifyConnection: func(connection tls.ConnectionState) error {
				digest := sha256.Sum256(connection.PeerCertificates[0].Raw)
				if hex.EncodeToString(digest[:]) != pin {
					return fmt.Errorf("certificate pin mismatch")
				}
				return nil
			},
		},
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "tcp4", l.listener.Addr().String())
		},
	}
	t.Cleanup(transport.CloseIdleConnections)
	return &http.Client{Transport: transport, Timeout: 3 * time.Second}
}

func TestRemoteTLSCertificateIsPinnedAndStableAcrossRestart(t *testing.T) {
	config := remoteTestConfig(t)
	config["remote_listen"] = "100.64.0.1:18889"
	config["tls_directory"] = filepath.Join(t.TempDir(), "tls")
	var publicPEM, privatePEM []byte
	var firstFingerprint string
	for attempt := 0; attempt < 2; attempt++ {
		binding := &remoteLoopback{}
		server, err := start(encodeRemoteConfig(t, config), binding.listen)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(server.Stop)
		var status struct {
			Running       bool   `json:"running"`
			Address       string `json:"address"`
			RemoteAddress string `json:"remote_address"`
			Fingerprint   string `json:"certificate_sha256"`
		}
		if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
			t.Fatal(err)
		}
		if !status.Running || status.Address != server.Address() || !strings.HasPrefix(status.Address, "127.0.0.1:") || status.RemoteAddress != config["remote_listen"] {
			t.Fatalf("listener status = %+v", status)
		}
		block, rest := pem.Decode([]byte(server.Certificate()))
		if block == nil || block.Type != "CERTIFICATE" || len(strings.TrimSpace(string(rest))) != 0 {
			t.Fatal("Certificate must contain exactly a public PEM certificate")
		}
		certificate, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			t.Fatal(err)
		}
		key, ok := certificate.PublicKey.(*ecdsa.PublicKey)
		if !ok || key.Curve != elliptic.P256() || len(certificate.IPAddresses) != 1 || !certificate.IPAddresses[0].Equal(net.ParseIP("100.64.0.1")) || len(certificate.DNSNames) != 0 {
			t.Fatal("certificate must be ECDSA P256 and restricted to the configured IP SAN")
		}
		if len(certificate.ExtKeyUsage) != 1 || certificate.ExtKeyUsage[0] != x509.ExtKeyUsageServerAuth || certificate.IsCA || certificate.KeyUsage&x509.KeyUsageDigitalSignature == 0 || certificate.NotAfter.Before(time.Now().AddDate(4, 0, 0)) {
			t.Fatal("certificate does not have a multi-year server-only profile")
		}
		if err := certificate.CheckSignature(certificate.SignatureAlgorithm, certificate.RawTBSCertificate, certificate.Signature); err != nil {
			t.Fatalf("certificate is not self-signed: %v", err)
		}
		digest := sha256.Sum256(certificate.Raw)
		if status.Fingerprint != hex.EncodeToString(digest[:]) {
			t.Fatalf("public fingerprint = %q", status.Fingerprint)
		}
		client := binding.client(t, server, status.Fingerprint)
		response, err := client.Get("https://100.64.0.1:18889/dataset/")
		if err != nil {
			t.Fatalf("verified TLS request failed: %v", err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		if response.TLS == nil {
			t.Fatal("remote listener served without TLS")
		}
		if response.StatusCode != http.StatusUnauthorized && response.StatusCode != http.StatusForbidden {
			t.Fatalf("unexpected remote boundary response: %d", response.StatusCode)
		}
		badPin := binding.client(t, server, strings.Repeat("0", 64))
		if response, err := badPin.Get("https://100.64.0.1:18889/dataset/"); err == nil {
			response.Body.Close()
			t.Fatal("incorrect certificate pin was accepted")
		}
		for _, secret := range []string{remoteTestPassword, config["control_root"].(string), config["tls_directory"].(string)} {
			if strings.Contains(server.Status(), secret) || strings.Contains(server.Certificate(), secret) {
				t.Fatal("public interface leaked private configuration")
			}
		}
		certFile, err := os.ReadFile(filepath.Join(config["tls_directory"].(string), "server.crt"))
		if err != nil || string(certFile) != server.Certificate() {
			t.Fatalf("persisted public certificate mismatch: %v", err)
		}
		keyFile, err := os.ReadFile(filepath.Join(config["tls_directory"].(string), "server.key"))
		if err != nil {
			t.Fatal(err)
		}
		if attempt == 0 {
			publicPEM, privatePEM, firstFingerprint = certFile, keyFile, status.Fingerprint
		} else if string(publicPEM) != string(certFile) || string(privatePEM) != string(keyFile) || firstFingerprint != status.Fingerprint {
			t.Fatal("restart silently rotated a stable certificate")
		}
		server.Stop()
	}
}

func TestRemoteExistingIdentityFailsClosedWithoutReplacingFiles(t *testing.T) {
	for _, defect := range []string{"certificate-only", "key-only", "damaged-certificate", "damaged-key", "expired", "not-yet-valid", "wrong-ip", "mismatched-key", "missing-server-auth", "certificate-with-trailing-data", "key-with-trailing-data"} {
		t.Run(defect, func(t *testing.T) {
			config := remoteTestConfig(t)
			config["remote_listen"] = "100.64.0.1:18889"
			directory := filepath.Join(t.TempDir(), "tls")
			config["tls_directory"] = directory
			binding := &remoteLoopback{}
			server, err := start(encodeRemoteConfig(t, config), binding.listen)
			if err != nil {
				t.Fatal(err)
			}
			server.Stop()
			certificatePath, keyPath := filepath.Join(directory, "server.crt"), filepath.Join(directory, "server.key")
			read := func(path string) []byte {
				t.Helper()
				data, err := os.ReadFile(path)
				if err != nil {
					t.Fatal(err)
				}
				return data
			}
			write := func(path string, data []byte) {
				t.Helper()
				if err := os.WriteFile(path, data, 0600); err != nil {
					t.Fatal(err)
				}
			}
			switch defect {
			case "certificate-only":
				err = os.Remove(keyPath)
			case "key-only":
				err = os.Remove(certificatePath)
			case "damaged-certificate":
				write(certificatePath, []byte("private-corrupt-certificate-marker"))
			case "damaged-key":
				write(keyPath, []byte("private-corrupt-key-marker"))
			case "mismatched-key":
				key, keyErr := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
				if keyErr != nil {
					t.Fatal(keyErr)
				}
				der, keyErr := x509.MarshalECPrivateKey(key)
				if keyErr != nil {
					t.Fatal(keyErr)
				}
				write(keyPath, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der}))
			case "certificate-with-trailing-data":
				write(certificatePath, append(read(certificatePath), []byte("invalid trailing certificate data")...))
			case "key-with-trailing-data":
				write(keyPath, append(read(keyPath), []byte("invalid trailing private data")...))
			default:
				pair, pairErr := tls.X509KeyPair(read(certificatePath), read(keyPath))
				if pairErr != nil {
					t.Fatal(pairErr)
				}
				certificate, parseErr := x509.ParseCertificate(pair.Certificate[0])
				if parseErr != nil {
					t.Fatal(parseErr)
				}
				switch defect {
				case "expired":
					certificate.NotBefore, certificate.NotAfter = time.Now().AddDate(-2, 0, 0), time.Now().Add(-time.Hour)
				case "not-yet-valid":
					certificate.NotBefore = time.Now().Add(time.Hour)
				case "wrong-ip":
					certificate.IPAddresses = []net.IP{net.ParseIP("100.64.0.2")}
				case "missing-server-auth":
					certificate.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
				}
				der, signErr := x509.CreateCertificate(rand.Reader, certificate, certificate, certificate.PublicKey, pair.PrivateKey)
				if signErr != nil {
					t.Fatal(signErr)
				}
				write(certificatePath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))
			}
			if err != nil {
				t.Fatal(err)
			}
			before := map[string][]byte{}
			for _, path := range []string{certificatePath, keyPath} {
				data, err := os.ReadFile(path)
				if !os.IsNotExist(err) && err != nil {
					t.Fatal(err)
				}
				if err == nil {
					before[path] = data
				}
			}
			binds := 0
			server, err = start(encodeRemoteConfig(t, config), func(network, address string) (net.Listener, error) {
				binds++
				return binding.listen(network, address)
			})
			if err == nil {
				server.Stop()
				t.Fatal("invalid existing TLS identity was accepted")
			}
			if binds != 0 {
				t.Fatal("invalid identity opened a listener before failing closed")
			}
			for _, secret := range []string{directory, remoteTestPassword, "private-corrupt"} {
				if strings.Contains(err.Error(), secret) {
					t.Fatal("TLS identity failure leaked private details")
				}
			}
			for _, path := range []string{certificatePath, keyPath} {
				data, err := os.ReadFile(path)
				original, existed := before[path]
				if existed && (err != nil || string(data) != string(original)) || !existed && !os.IsNotExist(err) {
					t.Fatal("fail-closed startup replaced or repaired an existing identity")
				}
			}
		})
	}
}

func TestRemoteHandlerFiltersSocketSourceWithoutTrustingForwardedHeaders(t *testing.T) {
	config := remoteTestConfig(t)
	config["remote_listen"], config["tls_directory"] = "100.64.0.1:18889", filepath.Join(t.TempDir(), "tls")
	binding := &remoteLoopback{}
	server, err := start(encodeRemoteConfig(t, config), binding.listen)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	for _, source := range []string{
		"100.64.0.0:12345", "100.64.1.2:54321", "100.127.255.255:12345",
		"127.0.0.1:12345", "192.168.1.2:12345", "10.1.2.3:12345", "172.16.0.1:12345",
		"100.63.255.255:12345", "100.128.0.0:12345", "8.8.8.8:12345", "[::1]:12345",
		"[::ffff:100.64.0.1]:12345", "[fd7a:115c:a1e0::1]:12345", "localhost:12345", "100.064.0.1:12345",
		"100.64.0.1", "100.64.0.1:not-a-port", "100.64.0.1:0", "",
	} {
		t.Run(source, func(t *testing.T) {
			request := httptest.NewRequest("GET", "https://100.64.0.1:18889/dataset/", nil)
			request.RemoteAddr = source
			request.Header.Set("X-Forwarded-For", "100.64.1.2")
			request.Header.Set("Forwarded", "for=100.64.1.2")
			recorder := httptest.NewRecorder()
			server.remote.Handler.ServeHTTP(recorder, request)
			want := http.StatusForbidden
			if source == "100.64.0.0:12345" || source == "100.64.1.2:54321" || source == "100.127.255.255:12345" {
				want = http.StatusUnauthorized
			}
			if recorder.Code != want {
				t.Fatalf("source %q returned %d, want %d", source, recorder.Code, want)
			}
		})
	}
	// Production filtering must also reject a real loopback peer after TLS,
	// even though this test's binding adapter made the listener reachable here.
	var status struct {
		Fingerprint string `json:"certificate_sha256"`
	}
	json.Unmarshal([]byte(server.Status()), &status)
	client := binding.client(t, server, status.Fingerprint)
	response, err := client.Get("https://100.64.0.1:18889/dataset/")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusForbidden {
		t.Fatalf("real loopback peer bypassed remote filtering: %d", response.StatusCode)
	}
}

func TestDualListenerBindFailureClosesEarlierSocket(t *testing.T) {
	for _, failed := range []string{"loopback", "remote"} {
		t.Run(failed, func(t *testing.T) {
			config := remoteTestConfig(t)
			config["remote_listen"], config["tls_directory"] = "100.64.0.1:18889", filepath.Join(t.TempDir(), "tls")
			var first net.Listener
			calls := 0
			server, err := start(encodeRemoteConfig(t, config), func(network, address string) (net.Listener, error) {
				calls++
				if failed == "loopback" || calls == 2 {
					return nil, fmt.Errorf("bind failure at private-path-marker with %s", remoteTestPassword)
				}
				var err error
				first, err = net.Listen("tcp4", "127.0.0.1:0")
				return first, err
			})
			if err == nil || server != nil {
				if server != nil {
					server.Stop()
				}
				t.Fatal("partially bound server was published")
			}
			if strings.Contains(err.Error(), "private-path-marker") || strings.Contains(err.Error(), remoteTestPassword) {
				t.Fatal("bind failure exposed the underlying error")
			}
			if first != nil {
				rebound, err := net.Listen("tcp4", first.Addr().String())
				if err != nil {
					t.Fatalf("startup left its first socket bound: %v", err)
				}
				rebound.Close()
			}
		})
	}
}

func TestUnexpectedListenerLossStopsBothEntrypointsAndEngine(t *testing.T) {
	for _, lost := range []string{"loopback", "remote"} {
		t.Run(lost, func(t *testing.T) {
			config := remoteTestConfig(t)
			config["remote_listen"], config["tls_directory"] = "100.64.0.1:18889", filepath.Join(t.TempDir(), "tls")
			binding := &remoteLoopback{}
			var local net.Listener
			server, err := start(encodeRemoteConfig(t, config), func(network, address string) (net.Listener, error) {
				listener, err := binding.listen(network, address)
				if !strings.HasPrefix(address, "100.") {
					local = listener
				}
				return listener, err
			})
			if err != nil {
				t.Fatal(err)
			}
			defer server.Stop()
			victim := local
			if lost == "remote" {
				victim = binding.listener
			}
			if err := victim.Close(); err != nil {
				t.Fatal(err)
			}
			deadline := time.Now().Add(2 * time.Second)
			for {
				var status struct {
					Running   bool   `json:"running"`
					LastError string `json:"last_error"`
				}
				if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
					t.Fatal(err)
				}
				request := httptest.NewRequest("GET", "/dataset/", nil)
				request.SetBasicAuth("remote-contract", remoteTestPassword)
				recorder := httptest.NewRecorder()
				server.engine.ServeHTTP(recorder, request)
				if !status.Running && status.LastError == "listener stopped unexpectedly" && recorder.Code == http.StatusServiceUnavailable {
					break
				}
				if time.Now().After(deadline) {
					t.Fatalf("listener loss left a live engine or incorrect status: %+v, engine=%d", status, recorder.Code)
				}
				time.Sleep(5 * time.Millisecond)
			}
			for _, address := range []string{local.Addr().String(), binding.listener.Addr().String()} {
				rebound, err := net.Listen("tcp4", address)
				if err != nil {
					t.Fatalf("listener loss left a sibling socket bound: %v", err)
				}
				rebound.Close()
			}
		})
	}
}

func TestRevokeRemoteIsImmediateAndPreservesLocalEngineAndCertificate(t *testing.T) {
	config := remoteTestConfig(t)
	config["remote_listen"], config["tls_directory"] = "100.64.0.1:18889", filepath.Join(t.TempDir(), "tls")
	binding := &remoteLoopback{}
	server, err := start(encodeRemoteConfig(t, config), binding.listen)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	public := server.Certificate()
	revoker, ok := any(server).(interface{ RevokeRemote() })
	if !ok {
		t.Fatal("Server must expose the thread-safe RevokeRemote policy boundary")
	}
	revoker.RevokeRemote()
	revoker.RevokeRemote()
	var status struct {
		Running       bool   `json:"running"`
		RemoteAddress string `json:"remote_address"`
		LastError     string `json:"last_error"`
		Fingerprint   string `json:"certificate_sha256"`
	}
	if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
		t.Fatal(err)
	}
	if !status.Running || status.RemoteAddress != "" || status.LastError != "" || status.Fingerprint == "" || server.Certificate() != public {
		t.Fatalf("active policy revocation changed local lifecycle or identity: %+v", status)
	}
	request := httptest.NewRequest("MKCOL", "https://100.64.0.1:18889/dataset/revoked", nil)
	request.RemoteAddr = "100.64.1.2:12345"
	request.SetBasicAuth("remote-contract", remoteTestPassword)
	recorder := httptest.NewRecorder()
	server.remote.Handler.ServeHTTP(recorder, request)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("revoked remote handler returned %d", recorder.Code)
	}
	request, _ = http.NewRequest("MKCOL", "http://"+server.Address()+"/dataset/local-still-active", nil)
	request.SetBasicAuth("remote-contract", remoteTestPassword)
	response, err := (&http.Client{Timeout: 2 * time.Second}).Do(request)
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusCreated {
		t.Fatalf("revocation stopped the local engine: %d", response.StatusCode)
	}
	rebound, err := net.Listen("tcp4", binding.listener.Addr().String())
	if err != nil {
		t.Fatalf("revocation did not close the remote socket: %v", err)
	}
	rebound.Close()
}

func TestBothEntrypointsShareAuthenticatedDataLocksAndAccounting(t *testing.T) {
	config := remoteTestConfig(t)
	config["remote_listen"], config["tls_directory"] = "100.64.0.1:18889", filepath.Join(t.TempDir(), "tls")
	binding := &remoteLoopback{trustedPeer: true}
	server, err := start(encodeRemoteConfig(t, config), binding.listen)
	if err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	var status struct {
		Fingerprint string `json:"certificate_sha256"`
		Requests    int64  `json:"requests"`
		Received    int64  `json:"bytes_received"`
		Sent        int64  `json:"bytes_sent"`
	}
	if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
		t.Fatal(err)
	}
	remoteClient := binding.client(t, server, status.Fingerprint)
	localClient := &http.Client{Timeout: 3 * time.Second}
	perform := func(client *http.Client, method, address, body string, want int) string {
		t.Helper()
		request, err := http.NewRequest(method, address, strings.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		request.SetBasicAuth("remote-contract", remoteTestPassword)
		response, err := client.Do(request)
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		data, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal(err)
		}
		if response.StatusCode != want {
			t.Fatalf("%s returned %d %s, want %d", method, response.StatusCode, data, want)
		}
		return string(data)
	}
	perform(remoteClient, "PUT", "https://100.64.0.1:18889/dataset/shared", "shared-native-data", http.StatusCreated)
	if got := perform(localClient, "GET", "http://"+server.Address()+"/dataset/shared", "", http.StatusOK); got != "shared-native-data" {
		t.Fatalf("local did not see remote data: %q", got)
	}
	perform(localClient, "MKCOL", "http://"+server.Address()+"/.phonenas/dataset-lock", "", http.StatusCreated)
	perform(remoteClient, "MKCOL", "https://100.64.0.1:18889/.phonenas/dataset-lock", "", http.StatusMethodNotAllowed)
	if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
		t.Fatal(err)
	}
	if status.Requests != 4 || status.Received != int64(len("shared-native-data")) || status.Sent < int64(len("shared-native-data")) {
		t.Fatalf("shared accounting is incorrect: %+v", status)
	}
	server.Stop()
	if _, err := os.Stat(filepath.Join(config["control_root"].(string), "dataset-lock")); err != nil {
		t.Fatalf("Stop removed a persistent dataset lock: %v", err)
	}
}

func TestRemoteSlowUploadAndQueuedMutationCancelOnStopOrRevoke(t *testing.T) {
	for _, action := range []string{"stop", "revoke"} {
		t.Run(action, func(t *testing.T) {
			config := remoteTestConfig(t)
			config["remote_listen"], config["tls_directory"] = "100.64.0.1:18889", filepath.Join(t.TempDir(), "tls")
			shared := config["shares"].([]map[string]any)[0]["root"].(string)
			if err := os.WriteFile(filepath.Join(shared, "partial"), []byte("committed-before-upload"), 0600); err != nil {
				t.Fatal(err)
			}
			binding := &remoteLoopback{trustedPeer: true}
			server, err := start(encodeRemoteConfig(t, config), binding.listen)
			if err != nil {
				t.Fatal(err)
			}
			defer server.Stop()
			var status struct {
				Fingerprint   string `json:"certificate_sha256"`
				Requests      int64  `json:"requests"`
				Running       bool   `json:"running"`
				RemoteAddress string `json:"remote_address"`
				LastError     string `json:"last_error"`
			}
			if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
				t.Fatal(err)
			}
			client := binding.client(t, server, status.Fingerprint)
			tlsConfig := client.Transport.(*http.Transport).TLSClientConfig.Clone()
			tlsConfig.ServerName = "100.64.0.1"
			dial := func() *tls.Conn {
				t.Helper()
				connection, err := tls.DialWithDialer(&net.Dialer{Timeout: 2 * time.Second}, "tcp4", binding.listener.Addr().String(), tlsConfig)
				if err != nil {
					t.Fatal(err)
				}
				connection.SetDeadline(time.Now().Add(10 * time.Second))
				t.Cleanup(func() { connection.Close() })
				return connection
			}
			request, _ := http.NewRequest("PUT", "https://100.64.0.1:18889/dataset/partial", nil)
			request.SetBasicAuth("remote-contract", remoteTestPassword)
			connection := dial()
			if _, err := fmt.Fprintf(connection, "PUT /dataset/partial HTTP/1.1\r\nHost: 100.64.0.1:18889\r\nAuthorization: %s\r\nContent-Length: 1000\r\n\r\nshort", request.Header.Get("Authorization")); err != nil {
				t.Fatal(err)
			}
			deadline := time.Now().Add(2 * time.Second)
			for {
				entries, err := os.ReadDir(shared)
				if err != nil {
					t.Fatal(err)
				}
				staged := false
				for _, entry := range entries {
					staged = staged || strings.HasPrefix(entry.Name(), ".phonenas-temp-")
				}
				if staged {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("real remote TLS PUT did not stage")
				}
				time.Sleep(5 * time.Millisecond)
			}
			queued := dial()
			if _, err := fmt.Fprintf(queued, "MKCOL /dataset/queued HTTP/1.1\r\nHost: 100.64.0.1:18889\r\nAuthorization: %s\r\nContent-Length: 0\r\n\r\n", request.Header.Get("Authorization")); err != nil {
				t.Fatal(err)
			}
			deadline = time.Now().Add(2 * time.Second)
			for {
				if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
					t.Fatal(err)
				}
				if status.Requests >= 2 {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("remote MKCOL did not queue behind PUT")
				}
				time.Sleep(5 * time.Millisecond)
			}
			started := time.Now()
			if action == "revoke" {
				server.RevokeRemote()
			} else {
				server.Stop()
			}
			bound := 7 * time.Second
			if action == "revoke" {
				bound = time.Second
			}
			if time.Since(started) > bound {
				t.Fatalf("%s exceeded the bounded cancellation window", action)
			}
			deadline = time.Now().Add(2 * time.Second)
			for {
				entries, err := os.ReadDir(shared)
				if err != nil {
					t.Fatal(err)
				}
				staged := false
				for _, entry := range entries {
					staged = staged || strings.HasPrefix(entry.Name(), ".phonenas-temp-")
				}
				if !staged {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("cancelled remote upload left staging data")
				}
				time.Sleep(5 * time.Millisecond)
			}
			if action == "revoke" {
				request, _ := http.NewRequest("GET", "http://"+server.Address()+"/dataset/partial", nil)
				request.SetBasicAuth("remote-contract", remoteTestPassword)
				response, err := (&http.Client{Timeout: 2 * time.Second}).Do(request)
				if err != nil {
					t.Fatal(err)
				}
				io.Copy(io.Discard, response.Body)
				response.Body.Close()
				if response.StatusCode != http.StatusOK {
					t.Fatalf("revoke interrupted the local engine: %d", response.StatusCode)
				}
			}
			if _, err := os.Stat(filepath.Join(shared, "queued")); !os.IsNotExist(err) {
				t.Fatalf("cancelled queued mutation published a directory: %v", err)
			}
			if data, err := os.ReadFile(filepath.Join(shared, "partial")); err != nil || string(data) != "committed-before-upload" {
				t.Fatalf("cancelled PUT replaced committed data: %q %v", data, err)
			}
			if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
				t.Fatal(err)
			}
			if status.Running != (action == "revoke") || status.RemoteAddress != "" || status.LastError != "" {
				t.Fatalf("intentional %s produced incorrect lifecycle status: %+v", action, status)
			}
		})
	}
}

func TestRemoteRejectsUnsafeConfigurationBeforeCreatingTLS(t *testing.T) {
	for _, address := range []string{
		"0.0.0.0:18889", "127.0.0.1:18889", "192.168.1.2:18889", "10.0.0.1:18889",
		"172.16.0.1:18889", "8.8.8.8:18889", "100.63.255.255:18889", "100.128.0.0:18889",
		"[::]:18889", "[::1]:18889", "[::ffff:100.64.0.1]:18889", "localhost:18889",
		"example.invalid:18889", "100.064.0.1:18889", "100.64.0.1:0", "100.64.0.1:1024",
		"100.64.0.1:-1", "100.64.0.1:65536", "100.64.0.1:https", "100.64.0.1", "100.64.0.1:+18889",
	} {
		t.Run(address, func(t *testing.T) {
			config := remoteTestConfig(t)
			directory := filepath.Join(t.TempDir(), "unused-tls")
			config["remote_listen"], config["tls_directory"] = address, directory
			server, err := Start(encodeRemoteConfig(t, config))
			if err == nil {
				server.Stop()
				t.Fatal("unsafe remote address was accepted")
			}
			for _, secret := range []string{remoteTestPassword, directory, config["control_root"].(string)} {
				if strings.Contains(err.Error(), secret) {
					t.Fatal("configuration error exposed private configuration")
				}
			}
			if _, err := os.Lstat(directory); !os.IsNotExist(err) {
				t.Fatalf("unsafe address touched TLS storage: %v", err)
			}
		})
	}
}

func TestRemoteRejectsUnsafeTLSDirectory(t *testing.T) {
	for _, kind := range []string{"missing", "relative", "unclean", "file", "share", "inside-share", "control", "inside-control", "parent"} {
		t.Run(kind, func(t *testing.T) {
			config := remoteTestConfig(t)
			config["remote_listen"] = "100.64.0.1:18889"
			control := config["control_root"].(string)
			share := config["shares"].([]map[string]any)[0]["root"].(string)
			switch kind {
			case "missing":
				config["tls_directory"] = ""
			case "relative":
				config["tls_directory"] = "private/tls"
			case "unclean":
				config["tls_directory"] = t.TempDir() + string(filepath.Separator) + ".." + string(filepath.Separator) + "tls"
			case "file":
				file := filepath.Join(t.TempDir(), "not-a-directory")
				if err := os.WriteFile(file, []byte("untouched"), 0600); err != nil {
					t.Fatal(err)
				}
				config["tls_directory"] = file
			case "share":
				config["tls_directory"] = share
			case "inside-share":
				config["tls_directory"] = filepath.Join(share, "tls")
			case "control":
				config["tls_directory"] = control
			case "inside-control":
				config["tls_directory"] = filepath.Join(control, "tls")
			case "parent":
				config["tls_directory"] = filepath.Dir(control)
			}
			server, err := Start(encodeRemoteConfig(t, config))
			if err == nil {
				server.Stop()
				t.Fatal("unsafe TLS directory was accepted")
			}
			for _, secret := range []string{remoteTestPassword, control, share} {
				if strings.Contains(err.Error(), secret) {
					t.Fatal("TLS error exposed private configuration")
				}
			}
		})
	}
}

func TestLoopbackOnlyDoesNotTouchTLSDirectory(t *testing.T) {
	config := remoteTestConfig(t)
	unused := filepath.Join(t.TempDir(), "must-not-be-created")
	config["tls_directory"] = unused
	server, err := Start(encodeRemoteConfig(t, config))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	if _, err := os.Lstat(unused); !os.IsNotExist(err) {
		t.Fatalf("loopback-only start accessed TLS storage: %v", err)
	}
	var status map[string]any
	if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"remote_address", "certificate_sha256"} {
		if value, present := status[field]; !present || value != "" {
			t.Errorf("loopback-only %s = %v (present %t), want an empty public field", field, value, present)
		}
	}
	public, ok := any(server).(interface{ Certificate() string })
	if !ok || public.Certificate() != "" {
		t.Fatal("loopback-only server must expose an empty public Certificate")
	}
}
