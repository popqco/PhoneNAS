package mobile

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestActualListenerAndIdempotentStop(t *testing.T) {
	root := t.TempDir()
	control, shared := filepath.Join(root, "control"), filepath.Join(root, "shared")
	for _, path := range []string{control, shared} {
		if err := os.Mkdir(path, 0700); err != nil {
			t.Fatal(err)
		}
	}
	config, _ := json.Marshal(map[string]any{
		"listen": "127.0.0.1:0", "username": "probe", "password": "a-long-synthetic-password-for-contract",
		"control_root": control, "shares": []map[string]any{{"name": "test", "root": shared, "read_only": false}},
	})
	server, err := Start(string(config))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	response, err := http.Get("http://" + server.Address() + "/test/")
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(io.Discard, response.Body)
	response.Body.Close()
	if response.StatusCode != 401 {
		t.Fatalf("uncredentialed real listener returned %d", response.StatusCode)
	}
	var status struct {
		Running  bool  `json:"running"`
		Requests int64 `json:"requests"`
	}
	if err := json.Unmarshal([]byte(server.Status()), &status); err != nil {
		t.Fatal(err)
	}
	if !status.Running || status.Requests != 1 {
		t.Fatalf("actual status missing: %+v", status)
	}
	server.Stop()
	server.Stop()
	if err := json.Unmarshal([]byte(server.Status()), &status); err != nil || status.Running {
		t.Fatalf("stop must clear actual status: %+v %v", status, err)
	}
}

func TestStopCancelsQueuedMKCOLWithoutPublishingPartialPUT(t *testing.T) {
	root := t.TempDir()
	control, shared := filepath.Join(root, "control"), filepath.Join(root, "shared")
	for _, path := range []string{control, shared} {
		if err := os.Mkdir(path, 0700); err != nil {
			t.Fatal(err)
		}
	}
	password := "a-long-synthetic-password-for-contract"
	config, _ := json.Marshal(map[string]any{
		"listen": "127.0.0.1:0", "username": "probe", "password": password,
		"control_root": control, "shares": []map[string]any{{"name": "test", "root": shared}},
	})
	server, err := Start(string(config))
	if err != nil {
		t.Fatal(err)
	}
	defer server.Stop()
	auth := base64.StdEncoding.EncodeToString([]byte("probe:" + password))
	connection, err := net.Dial("tcp", server.Address())
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	_, err = fmt.Fprintf(connection, "PUT /test/partial HTTP/1.1\r\nHost: %s\r\nAuthorization: Basic %s\r\nContent-Length: 1000\r\n\r\nshort", server.Address(), auth)
	if err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		entries, err := os.ReadDir(shared)
		if err != nil {
			t.Fatal(err)
		}
		started := false
		for _, entry := range entries {
			started = started || strings.HasPrefix(entry.Name(), ".phonenas-temp-")
		}
		if started {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("slow PUT did not enter staging")
		}
		time.Sleep(10 * time.Millisecond)
	}
	queued, err := net.Dial("tcp", server.Address())
	if err != nil {
		t.Fatal(err)
	}
	defer queued.Close()
	_, err = fmt.Fprintf(queued, "MKCOL /test/should-not-exist HTTP/1.1\r\nHost: %s\r\nAuthorization: Basic %s\r\nContent-Length: 0\r\n\r\n", server.Address(), auth)
	if err != nil {
		t.Fatal(err)
	}
	deadline = time.Now().Add(3 * time.Second)
	for {
		var status struct {
			Requests int64 `json:"requests"`
		}
		json.Unmarshal([]byte(server.Status()), &status)
		if status.Requests >= 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("MKCOL did not enter request queue")
		}
		time.Sleep(10 * time.Millisecond)
	}
	stopDone := make(chan struct{})
	go func() { server.Stop(); close(stopDone) }()
	// Complete the body while Stop is draining, before the socket is forcibly
	// closed. Neither this upload nor the already queued mutation may commit.
	deadline = time.Now().Add(2 * time.Second)
	for server.running.Load() && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if server.running.Load() {
		t.Fatal("Stop did not start")
	}
	if _, err := io.WriteString(connection, strings.Repeat("x", 995)); err != nil {
		t.Fatal(err)
	}
	select {
	case <-stopDone:
	case <-time.After(7 * time.Second):
		t.Fatal("Stop did not drain")
	}
	for _, name := range []string{"partial", "should-not-exist"} {
		if _, err := os.Stat(filepath.Join(shared, name)); !os.IsNotExist(err) {
			t.Fatalf("Stop left an uncommitted resource %s: %v", name, err)
		}
	}
}

func TestDebugBridgeRejectsWildcardBeforeOpeningRoots(t *testing.T) {
	_, err := Start(`{"listen":"0.0.0.0:18888","username":"debug","password":"never-log-this"}`)
	if err == nil {
		t.Fatal("debug bridge must never bind every interface")
	}
}
