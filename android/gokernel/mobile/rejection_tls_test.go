package mobile

import (
	"bufio"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func rejectedTLSFixture(t *testing.T) (*Server, func() *tls.Conn) {
	t.Helper()
	config := remoteTestConfig(t)
	config["remote_listen"], config["tls_directory"] = "100.64.0.1:18889", filepath.Join(t.TempDir(), "tls")
	binding := &remoteLoopback{trustedPeer: true}
	server, err := start(encodeRemoteConfig(t, config), binding.listen)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(server.Stop)
	var status struct {
		Fingerprint string `json:"certificate_sha256"`
	}
	if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
		t.Fatal(err)
	}
	client := binding.client(t, server, status.Fingerprint)
	tlsConfig := client.Transport.(*http.Transport).TLSClientConfig.Clone()
	tlsConfig.ServerName = "100.64.0.1"
	return server, func() *tls.Conn {
		t.Helper()
		connection, err := tls.DialWithDialer(&net.Dialer{Timeout: 2 * time.Second}, "tcp4", binding.listener.Addr().String(), tlsConfig)
		if err != nil {
			t.Fatal(err)
		}
		if err := connection.SetDeadline(time.Now().Add(3 * time.Second)); err != nil {
			connection.Close()
			t.Fatal(err)
		}
		return connection
	}
}

type rejectedTLSResult struct {
	status int
	err    error
}

func readRejectedTLS(connection *tls.Conn) rejectedTLSResult {
	response, err := http.ReadResponse(bufio.NewReader(connection), nil)
	if err != nil {
		return rejectedTLSResult{err: err}
	}
	_, err = io.Copy(io.Discard, response.Body)
	response.Body.Close()
	return rejectedTLSResult{status: response.StatusCode, err: err}
}

func TestRejectedTLSBodiesHaveBoundedDrainAndDoNotHoldEngineMutex(t *testing.T) {
	server, dial := rejectedTLSFixture(t)
	for _, test := range []struct {
		name, path, password, framing, initial string
		want                                   int
	}{
		{"slow-invalid-auth", "/dataset/rejected", "wrong-synthetic-password", "Content-Length: 2\r\n", "n", http.StatusUnauthorized},
		{"slow-reserved-path", "/dataset/.phonenas-temp-rejected", remoteTestPassword, "Content-Length: 2\r\n", "n", http.StatusForbidden},
		{"large-invalid-auth", "/dataset/rejected", "wrong-synthetic-password", "Content-Length: 65537\r\n", "n", http.StatusUnauthorized},
		{"large-reserved-path", "/dataset/.phonenas-temp-rejected", remoteTestPassword, "Content-Length: 65537\r\n", "n", http.StatusForbidden},
		{"unknown-reserved-path", "/dataset/.phonenas-temp-rejected", remoteTestPassword, "Transfer-Encoding: chunked\r\n", "1\r\nn\r\n", http.StatusForbidden},
		{"expect-invalid-auth", "/dataset/rejected", "wrong-synthetic-password", "Content-Length: 2\r\nExpect: 100-continue\r\n", "", http.StatusUnauthorized},
	} {
		t.Run(test.name, func(t *testing.T) {
			connection := dial()
			defer connection.Close()
			request, _ := http.NewRequest("PUT", "https://100.64.0.1:18889"+test.path, nil)
			request.SetBasicAuth("remote-contract", test.password)
			started := time.Now()
			if _, err := fmt.Fprintf(connection, "PUT %s HTTP/1.1\r\nHost: 100.64.0.1:18889\r\nAuthorization: %s\r\n%s\r\n%s", test.path, request.Header.Get("Authorization"), test.framing, test.initial); err != nil {
				t.Fatal(err)
			}
			if strings.HasPrefix(test.name, "slow-") {
				// A slow rejected body may use a bounded admission slot, but must
				// not keep the engine mutex and block unrelated authenticated work.
				time.Sleep(25 * time.Millisecond)
				local, _ := http.NewRequest("OPTIONS", "http://"+server.Address()+"/dataset/", nil)
				local.SetBasicAuth("remote-contract", remoteTestPassword)
				response, err := (&http.Client{Timeout: 150 * time.Millisecond}).Do(local)
				if err != nil {
					t.Fatalf("rejected-body drain held the engine mutex: %v", err)
				}
				io.Copy(io.Discard, response.Body)
				response.Body.Close()
				if response.StatusCode != http.StatusOK {
					t.Fatalf("unrelated local request = %d", response.StatusCode)
				}
			}
			result := readRejectedTLS(connection)
			if result.err != nil || result.status != test.want {
				t.Fatalf("bounded rejection = %d %v, want %d", result.status, result.err, test.want)
			}
			if elapsed := time.Since(started); elapsed > 750*time.Millisecond {
				t.Fatalf("rejected body consumed an unbounded read window: %v", elapsed)
			}
		})
	}
}

func TestRejectedTLSFiniteCloseRequestsHaveNoTransportRetry(t *testing.T) {
	_, dial := rejectedTLSFixture(t)
	for attempt := 0; attempt < 100; attempt++ {
		for _, test := range []struct {
			method, path, password, body string
			want                         int
		}{
			{"GET", "/dataset/missing", remoteTestPassword, "", http.StatusNotFound},
			{"PUT", "/dataset/rejected", "wrong-synthetic-password", "no", http.StatusUnauthorized},
			{"PUT", "/dataset/.phonenas-temp-rejected", remoteTestPassword, "no", http.StatusForbidden},
		} {
			connection := dial()
			request, _ := http.NewRequest(test.method, "https://100.64.0.1:18889"+test.path, nil)
			request.SetBasicAuth("remote-contract", test.password)
			_, err := fmt.Fprintf(connection, "%s %s HTTP/1.1\r\nHost: 100.64.0.1:18889\r\nAuthorization: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n", test.method, test.path, request.Header.Get("Authorization"), len(test.body))
			if err == nil && test.body != "" {
				_, err = io.WriteString(connection, test.body)
			}
			if err != nil {
				connection.Close()
				t.Fatalf("attempt %d %s send failed without retry: %v", attempt, test.method, err)
			}
			result := readRejectedTLS(connection)
			connection.Close()
			if result.err != nil || result.status != test.want {
				t.Fatalf("attempt %d %s returned %d %v, want %d without retry", attempt, test.method, result.status, result.err, test.want)
			}
		}
	}
}

func TestRejectedTLSSmallSplitPUTDrainsBeforeClosing(t *testing.T) {
	server, dial := rejectedTLSFixture(t)
	for _, test := range []struct {
		name     string
		path     string
		password string
		want     int
	}{
		{"invalid-auth", "/dataset/rejected", "wrong-synthetic-password", http.StatusUnauthorized},
		{"reserved-path", "/dataset/.phonenas-temp-rejected", remoteTestPassword, http.StatusForbidden},
	} {
		t.Run(test.name, func(t *testing.T) {
			connection := dial()
			defer connection.Close()
			request, _ := http.NewRequest("PUT", "https://100.64.0.1:18889"+test.path, nil)
			request.SetBasicAuth("remote-contract", test.password)
			if _, err := fmt.Fprintf(connection, "PUT %s HTTP/1.1\r\nHost: 100.64.0.1:18889\r\nAuthorization: %s\r\nContent-Length: 2\r\nConnection: close\r\n\r\nn", test.path, request.Header.Get("Authorization")); err != nil {
				t.Fatal(err)
			}
			result := make(chan rejectedTLSResult, 1)
			go func() { result <- readRejectedTLS(connection) }()
			var response rejectedTLSResult
			early := false
			select {
			case response = <-result:
				early = true
				t.Errorf("TLS rejection closed before the second byte of a small declared body arrived: status=%d error=%v", response.status, response.err)
			case <-time.After(25 * time.Millisecond):
			}
			if _, err := io.WriteString(connection, "o"); err != nil {
				t.Fatalf("TLS peer closed while the small split body was still being sent: %v", err)
			}
			if !early {
				response = <-result
			}
			if response.err != nil || response.status != test.want {
				t.Fatalf("TLS rejection headers/body = %d %v, want %d without transport failure", response.status, response.err, test.want)
			}
		})
	}
	var status struct {
		Received int64 `json:"bytes_received"`
	}
	if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
		t.Fatal(err)
	}
	if status.Received != 4 {
		t.Fatalf("rejected small bodies were not drained through production accounting: received=%d, want 4", status.Received)
	}
}
