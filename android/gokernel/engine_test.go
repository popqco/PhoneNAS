package gokernel_test

import (
	"bufio"
	"encoding/base64"
	"encoding/xml"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"

	kernel "phonenas.local/gokernel"
)

const password = "test-password-32-characters-long!"

type fixture struct {
	engine *kernel.Engine
	server *httptest.Server
	config kernel.Config
	root   string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	root := t.TempDir()
	c := kernel.Config{Username: "owner", Password: password, ControlRoot: t.TempDir(), Shares: []kernel.Share{{Name: "dataset", Root: root}}}
	e, err := kernel.New(c)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(e)
	t.Cleanup(func() { srv.Close(); _ = e.Close() })
	return &fixture{e, srv, c, root}
}

func request(t *testing.T, srv *httptest.Server, method, path string, body io.Reader, headers map[string]string) (int, http.Header, []byte) {
	t.Helper()
	req, err := http.NewRequest(method, srv.URL+path, body)
	if err != nil {
		t.Fatal(err)
	}
	req.SetBasicAuth("owner", password)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	res, err := srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	data, err := io.ReadAll(res.Body)
	if err != nil {
		t.Fatal(err)
	}
	return res.StatusCode, res.Header, data
}

func expectStatus(t *testing.T, srv *httptest.Server, method, path string, want int) []byte {
	t.Helper()
	status, _, body := request(t, srv, method, path, nil, nil)
	if status != want {
		t.Fatalf("%s %s = %d %q, want %d", method, path, status, body, want)
	}
	return body
}

func TestVirtualRootAndReservedEntries(t *testing.T) {
	f := newFixture(t)
	for _, name := range []string{"visible.txt", ".phonenas-temp-previous-owner", ".phonenas"} {
		if err := os.WriteFile(filepath.Join(f.root, name), []byte(name), 0600); err != nil {
			t.Fatal(err)
		}
	}
	status, _, body := request(t, f.server, "PROPFIND", "/", nil, map[string]string{"Depth": "1"})
	if status != 207 {
		t.Fatalf("virtual root = %d %q", status, body)
	}
	var result struct {
		Responses []struct {
			Href string `xml:"href"`
		} `xml:"response"`
	}
	if err := xml.Unmarshal(body, &result); err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, r := range result.Responses {
		seen[r.Href] = true
	}
	if len(seen) != 3 || !seen["/"] || !seen["/dataset/"] || !seen["/.phonenas/"] {
		t.Fatalf("virtual root entries = %#v", seen)
	}
	status, _, body = request(t, f.server, "PROPFIND", "/dataset/", nil, map[string]string{"Depth": "1"})
	if status != 207 || !strings.Contains(string(body), "visible.txt") || strings.Contains(string(body), ".phonenas") || strings.Contains(string(body), f.root) {
		t.Fatalf("filtered export = %d %q", status, body)
	}
	for _, path := range []string{"/dataset/.phonenas", "/dataset/.phonenas-temp-previous-owner"} {
		expectStatus(t, f.server, "GET", path, http.StatusForbidden)
	}
	status, _, _ = request(t, f.server, "PROPFIND", "/dataset/", nil, map[string]string{"Depth": "infinity"})
	if status != http.StatusForbidden {
		t.Fatalf("unbounded depth = %d", status)
	}
}

func TestConfigRejectsUnsafeRootsAndNames(t *testing.T) {
	base := t.TempDir()
	child := filepath.Join(base, "child")
	if err := os.Mkdir(child, 0700); err != nil {
		t.Fatal(err)
	}
	control := t.TempDir()
	for _, shares := range [][]kernel.Share{
		{{Name: "one", Root: base}, {Name: "two", Root: base}},
		{{Name: "one", Root: base}, {Name: "two", Root: child}},
		{{Name: "one", Root: base}, {Name: "one", Root: t.TempDir()}},
		{{Name: ".phonenas", Root: base}},
		{{Name: ".phonenas-temp-export", Root: base}},
		{{Name: "../escape", Root: base}},
		{{Name: "a/b", Root: base}},
	} {
		e, err := kernel.New(kernel.Config{Username: "owner", Password: password, ControlRoot: control, Shares: shares})
		if err == nil {
			_ = e.Close()
			t.Fatalf("accepted unsafe shares %#v", shares)
		}
	}
	e, err := kernel.New(kernel.Config{Username: "owner", Password: password, ControlRoot: base, Shares: []kernel.Share{{Name: "one", Root: child}}})
	if err == nil {
		_ = e.Close()
		t.Fatal("accepted control/share overlap")
	}
	e, err = kernel.New(kernel.Config{Username: "owner", Password: "short", ControlRoot: control})
	if err == nil {
		_ = e.Close()
		t.Fatal("accepted short password")
	}
}

func TestPathRejections(t *testing.T) {
	f := newFixture(t)
	for _, path := range []string{"/dataset/../x", "/dataset/%2e%2e/x", "/dataset/./x", "/dataset//x", "/dataset/a%2fb", "/dataset/a%5cb", "/dataset/a%00b", "/dataset/%ff", "/dataset/.phonenas/locks", "/dataset/.PHONENAS-temp-hidden"} {
		status, _, _ := request(t, f.server, "GET", path, nil, nil)
		if status != http.StatusBadRequest && status != http.StatusForbidden {
			t.Errorf("unsafe GET %s = %d", path, status)
		}
	}
}

func TestAtomicPUTAndInterruptedUpload(t *testing.T) {
	f := newFixture(t)
	for _, value := range []string{"old committed content", "replacement"} {
		status, _, _ := request(t, f.server, "PUT", "/dataset/file", strings.NewReader(value), nil)
		if status != http.StatusCreated && status != http.StatusNoContent {
			t.Fatalf("PUT = %d", status)
		}
		if got := expectStatus(t, f.server, "GET", "/dataset/file", 200); string(got) != value {
			t.Fatalf("PUT content = %q", got)
		}
	}
	for _, target := range []string{"file", "new-file"} {
		conn, err := net.Dial("tcp", f.server.Listener.Addr().String())
		if err != nil {
			t.Fatal(err)
		}
		_, err = fmt.Fprintf(conn, "PUT /dataset/%s HTTP/1.1\r\nHost: %s\r\nAuthorization: Basic %s\r\nContent-Length: 100\r\nConnection: close\r\n\r\nshort", target, f.server.Listener.Addr(), base64.StdEncoding.EncodeToString([]byte("owner:"+password)))
		if err != nil {
			t.Fatal(err)
		}
		if err := conn.(*net.TCPConn).CloseWrite(); err != nil {
			t.Fatal(err)
		}
		res, err := http.ReadResponse(bufio.NewReader(conn), nil)
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, res.Body)
		res.Body.Close()
		conn.Close()
		if res.StatusCode >= 200 && res.StatusCode < 300 {
			t.Fatalf("interrupted PUT returned %d", res.StatusCode)
		}
	}
	if got := expectStatus(t, f.server, "GET", "/dataset/file", 200); string(got) != "replacement" {
		t.Fatalf("interrupted replacement = %q", got)
	}
	expectStatus(t, f.server, "GET", "/dataset/new-file", 404)
	entries, err := os.ReadDir(f.root)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".phonenas-temp-") {
			t.Errorf("owned upload temp leaked: %s", entry.Name())
		}
	}
}

func TestMKCOLIsExclusiveAndSurvivesRestart(t *testing.T) {
	f := newFixture(t)
	expectStatus(t, f.server, "MKCOL", "/.phonenas", 405)
	expectStatus(t, f.server, "MKCOL", "/.phonenas/locks", 201)
	expectStatus(t, f.server, "MKCOL", "/.phonenas/missing/lock", 409)
	var wg sync.WaitGroup
	codes := make(chan int, 12)
	for i := 0; i < cap(codes); i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			status, _, _ := request(t, f.server, "MKCOL", "/.phonenas/locks/dataset", nil, nil)
			codes <- status
		}()
	}
	wg.Wait()
	close(codes)
	created := 0
	for code := range codes {
		if code == 201 {
			created++
		} else if code != 405 {
			t.Fatalf("concurrent MKCOL = %d", code)
		}
	}
	if created != 1 {
		t.Fatalf("concurrent winners = %d, want 1", created)
	}
	status, _, _ := request(t, f.server, "PUT", "/.phonenas/locks/dataset/owner.json", strings.NewReader("persistent owner"), nil)
	if status != 201 {
		t.Fatalf("owner PUT = %d", status)
	}
	f.server.Close()
	if err := f.engine.Close(); err != nil {
		t.Fatal(err)
	}
	e, err := kernel.New(f.config)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(e)
	defer func() { srv.Close(); _ = e.Close() }()
	expectStatus(t, srv, "MKCOL", "/.phonenas/locks/dataset", 405)
	if body := expectStatus(t, srv, "GET", "/.phonenas/locks/dataset/owner.json", 200); string(body) != "persistent owner" {
		t.Fatalf("owner lost after restart: %q", body)
	}
	status, _, _ = request(t, srv, "PUT", "/dataset/existing", strings.NewReader("file"), nil)
	if status != 201 {
		t.Fatal(status)
	}
	expectStatus(t, srv, "MKCOL", "/dataset/existing", 405)
}

func TestRangeUnicodePercentMOVEAndDelete(t *testing.T) {
	f := newFixture(t)
	name, moved := "中文%#?.txt", "移动%#?.txt"
	if runtime.GOOS == "windows" {
		name, moved = "中文%#.txt", "移动%#.txt"
	}
	src := "/dataset/" + url.PathEscape(name)
	dst := "/dataset/" + url.PathEscape(moved)
	status, _, _ := request(t, f.server, "PUT", src, strings.NewReader("0123456789"), nil)
	if status != 201 {
		t.Fatal(status)
	}
	status, hdr, body := request(t, f.server, "GET", src, nil, map[string]string{"Range": "bytes=2-5"})
	if status != 206 || string(body) != "2345" || hdr.Get("Content-Range") != "bytes 2-5/10" {
		t.Fatalf("range = %d %q %#v", status, body, hdr)
	}
	status, hdr, body = request(t, f.server, "HEAD", src, nil, nil)
	if status != 200 || len(body) != 0 || hdr.Get("Content-Length") != "10" {
		t.Fatalf("HEAD = %d %q %#v", status, body, hdr)
	}
	status, _, body = request(t, f.server, "PROPFIND", "/dataset/", nil, map[string]string{"Depth": "1"})
	if status != 207 {
		t.Fatalf("PROPFIND = %d %q", status, body)
	}
	var result struct {
		Responses []struct {
			Href string `xml:"href"`
		} `xml:"response"`
	}
	if err := xml.Unmarshal(body, &result); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, entry := range result.Responses {
		u, err := url.Parse(entry.Href)
		if err == nil && u.Path == "/dataset/"+name {
			found = true
			expectStatus(t, f.server, "GET", entry.Href, 200)
		}
	}
	if !found {
		t.Fatalf("roundtrip href missing: %s", body)
	}
	status, _, _ = request(t, f.server, "MOVE", src, nil, map[string]string{"Destination": f.server.URL + dst, "Overwrite": "T"})
	if status != 201 {
		t.Fatalf("MOVE = %d", status)
	}
	expectStatus(t, f.server, "GET", src, 404)
	if got := expectStatus(t, f.server, "GET", dst, 200); string(got) != "0123456789" {
		t.Fatal(string(got))
	}
	status, _, _ = request(t, f.server, "PUT", src, strings.NewReader("second"), nil)
	if status != 201 {
		t.Fatal(status)
	}
	status, _, _ = request(t, f.server, "MOVE", src, nil, map[string]string{"Destination": f.server.URL + dst, "Overwrite": "F"})
	if status != 412 {
		t.Fatalf("Overwrite F = %d", status)
	}
	if got := expectStatus(t, f.server, "GET", dst, 200); string(got) != "0123456789" {
		t.Fatal(string(got))
	}
	for dest, want := range map[string]int{"/.phonenas/x": 409, "https://other.invalid/dataset/x": 502, "/dataset/a%2fb": 400, "/dataset/": 403, "/dataset/.phonenas-temp-x": 403} {
		status, _, _ = request(t, f.server, "MOVE", src, nil, map[string]string{"Destination": dest})
		if status != want {
			t.Errorf("MOVE to %s = %d, want %d", dest, status, want)
		}
	}
	status, _, _ = request(t, f.server, "MOVE", src, nil, map[string]string{"Destination": f.server.URL + dst, "Overwrite": "T"})
	if status != 204 {
		t.Fatalf("overwrite MOVE = %d", status)
	}
	if got := expectStatus(t, f.server, "GET", dst, 200); string(got) != "second" {
		t.Fatal(string(got))
	}
	for _, p := range []string{"/", "/dataset", "/.phonenas"} {
		expectStatus(t, f.server, "DELETE", p, 403)
	}
	expectStatus(t, f.server, "DELETE", dst, 204)
	expectStatus(t, f.server, "GET", dst, 404)
	expectStatus(t, f.server, "DELETE", dst, 404)
}

func TestReadOnlyAndHonestCapabilities(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "file"), []byte("unchanged"), 0600); err != nil {
		t.Fatal(err)
	}
	e, err := kernel.New(kernel.Config{Username: "owner", Password: password, ControlRoot: t.TempDir(), Shares: []kernel.Share{{Name: "readonly", Root: root, ReadOnly: true}}})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(e)
	defer func() { srv.Close(); _ = e.Close() }()
	if got := expectStatus(t, srv, "GET", "/readonly/file", 200); string(got) != "unchanged" {
		t.Fatal(string(got))
	}
	for _, method := range []string{"PUT", "DELETE", "MKCOL", "MOVE", "COPY", "PROPPATCH", "LOCK", "UNLOCK"} {
		status, _, _ := request(t, srv, method, "/readonly/file", strings.NewReader("changed"), map[string]string{"Destination": srv.URL + "/readonly/new"})
		if status != 403 {
			t.Errorf("readonly %s = %d", method, status)
		}
	}
	if got := expectStatus(t, srv, "GET", "/readonly/file", 200); string(got) != "unchanged" {
		t.Fatal(string(got))
	}
	status, hdr, _ := request(t, srv, "OPTIONS", "/readonly/file", nil, nil)
	if status != 200 || strings.Contains(hdr.Get("Allow"), "PUT") || strings.Contains(hdr.Get("Allow"), "LOCK") || hdr.Get("DAV") != "1" {
		t.Fatalf("capabilities = %d %#v", status, hdr)
	}
	f := newFixture(t)
	expectStatus(t, f.server, "LOCK", "/dataset/file", 405)
	expectStatus(t, f.server, "UNLOCK", "/dataset/file", 405)
	expectStatus(t, f.server, "COPY", "/dataset/file", 501)
}

func TestConcurrentConditionalUploadsKeepOnlyTheWinningBytes(t *testing.T) {
	f := newFixture(t)
	const path = "/dataset/new.bin"
	start := make(chan struct{})
	type result struct {
		status int
		body   string
		err    error
	}
	results := make(chan result, 2)
	for _, byteValue := range []string{"A", "B"} {
		payload := strings.Repeat(byteValue, 192<<10)
		go func() {
			<-start
			req, err := http.NewRequest("PUT", f.server.URL+path, strings.NewReader(payload))
			if err != nil {
				results <- result{err: err}
				return
			}
			req.SetBasicAuth("owner", password)
			req.Header.Set("If-None-Match", "*")
			req.Header.Set("Expect", "100-continue")
			res, err := f.server.Client().Do(req)
			if err != nil {
				results <- result{err: err}
				return
			}
			_, err = io.Copy(io.Discard, res.Body)
			res.Body.Close()
			results <- result{res.StatusCode, payload, err}
		}()
	}
	close(start)
	first, second := <-results, <-results
	if first.err != nil || second.err != nil {
		t.Fatalf("conditional uploads: %v, %v", first.err, second.err)
	}
	if first.status == http.StatusPreconditionFailed {
		first, second = second, first
	}
	if first.status != http.StatusCreated || second.status != http.StatusPreconditionFailed {
		t.Fatalf("conditional uploads = %d, %d, want 201 and 412", first.status, second.status)
	}
	if got := expectStatus(t, f.server, "GET", path, 200); string(got) != first.body {
		t.Fatal("published bytes differ from the accepted upload")
	}
	entries, err := os.ReadDir(f.root)
	if err != nil || len(entries) != 1 || entries[0].Name() != "new.bin" {
		t.Fatalf("unexpected published/staged entries: %v %v", entries, err)
	}
}

func TestUploadLimitsAndTypeSafety(t *testing.T) {
	root := t.TempDir()
	e, err := kernel.New(kernel.Config{Username: "owner", Password: password, ControlRoot: t.TempDir(), Shares: []kernel.Share{{Name: "dataset", Root: root}}, MaxUploadBytes: 8})
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(e)
	defer func() { srv.Close(); _ = e.Close() }()
	for _, name := range []string{"file", "missing"} {
		if name == "file" {
			status, _, _ := request(t, srv, "PUT", "/dataset/file", strings.NewReader("old"), nil)
			if status != 201 {
				t.Fatal(status)
			}
		}
		for _, reader := range []io.Reader{strings.NewReader("123456789"), io.LimitReader(strings.NewReader("123456789"), 9)} {
			status, _, _ := request(t, srv, "PUT", "/dataset/"+name, reader, nil)
			if status != 413 {
				t.Fatalf("oversize %s = %d", name, status)
			}
		}
	}
	if got := expectStatus(t, srv, "GET", "/dataset/file", 200); string(got) != "old" {
		t.Fatal(string(got))
	}
	expectStatus(t, srv, "GET", "/dataset/missing", 404)
	expectStatus(t, srv, "MKCOL", "/dataset/dir", 201)
	status, _, _ := request(t, srv, "PUT", "/dataset/dir", strings.NewReader("x"), nil)
	if status != 409 {
		t.Fatalf("PUT directory = %d", status)
	}
	status, _, _ = request(t, srv, "PUT", "/dataset/file", strings.NewReader("x"), map[string]string{"If-None-Match": "*"})
	if status != 412 {
		t.Fatalf("create-only PUT = %d", status)
	}
	status, _, _ = request(t, srv, "MOVE", "/dataset/file", nil, map[string]string{"Destination": srv.URL + "/dataset/dir", "Overwrite": "T"})
	if status != 409 {
		t.Fatalf("MOVE file over dir = %d", status)
	}
	status, _, _ = request(t, srv, "MOVE", "/dataset/dir", nil, map[string]string{"Destination": srv.URL + "/dataset/file", "Overwrite": "T"})
	if status != 409 {
		t.Fatalf("MOVE dir over file = %d", status)
	}
}

func TestSymlinksNeverExportedOrFollowed(t *testing.T) {
	f := newFixture(t)
	outside := t.TempDir()
	if err := os.WriteFile(filepath.Join(outside, "secret"), []byte("outside"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(f.root, "alias")); err != nil {
		t.Skipf("symlink capability unavailable: %v", err)
	}
	for _, path := range []string{"/dataset/alias", "/dataset/alias/secret"} {
		for _, method := range []string{"GET", "PROPFIND", "PUT", "DELETE", "MKCOL"} {
			status, _, _ := request(t, f.server, method, path, nil, map[string]string{"Depth": "1"})
			if status != 403 {
				t.Errorf("%s symlink path = %d", method, status)
			}
		}
	}
	status, _, body := request(t, f.server, "PROPFIND", "/dataset/", nil, map[string]string{"Depth": "1"})
	if status != 207 || strings.Contains(string(body), "alias") {
		t.Fatalf("symlink exposed: %d %s", status, body)
	}
	if err := os.Symlink(f.root, filepath.Join(f.root, "internal")); err != nil {
		t.Fatal(err)
	}
	expectStatus(t, f.server, "GET", "/dataset/internal", 403)
	cfg := f.config
	cfg.Shares = []kernel.Share{{Name: "bad", Root: filepath.Join(f.root, "alias")}}
	if e, err := kernel.New(cfg); err == nil {
		_ = e.Close()
		t.Fatal("accepted symlink root")
	}
	expectStatus(t, f.server, "MKCOL", "/dataset/tree", 201)
	if err := os.Symlink(outside, filepath.Join(f.root, "tree", "alias")); err != nil {
		t.Fatal(err)
	}
	expectStatus(t, f.server, "DELETE", "/dataset/tree", 204)
	if got, err := os.ReadFile(filepath.Join(outside, "secret")); err != nil || string(got) != "outside" {
		t.Fatalf("outside modified: %q %v", got, err)
	}
}

func TestAuthenticatedExportRead(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "hello.txt"), []byte("hello from dataset"), 0600); err != nil {
		t.Fatal(err)
	}
	e, err := kernel.New(kernel.Config{Username: "owner", Password: password, ControlRoot: t.TempDir(), Shares: []kernel.Share{{Name: "dataset", Root: root}}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = e.Close() })
	srv := httptest.NewServer(e)
	t.Cleanup(srv.Close)
	for _, method := range []string{"OPTIONS", "GET", "PROPFIND"} {
		req, _ := http.NewRequest(method, srv.URL+"/dataset/hello.txt", nil)
		res, err := srv.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		if res.StatusCode != http.StatusUnauthorized || !strings.HasPrefix(res.Header.Get("WWW-Authenticate"), "Basic ") {
			t.Fatalf("anonymous %s = %d, want Basic 401", method, res.StatusCode)
		}
	}
	req, _ := http.NewRequest("GET", srv.URL+"/dataset/hello.txt", nil)
	req.SetBasicAuth("owner", password)
	res, err := srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(res.Body)
	if res.StatusCode != http.StatusOK || string(body) != "hello from dataset" {
		t.Fatalf("GET = %d %q", res.StatusCode, body)
	}
}
