package gokernel

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
)

const rootFaultPassword = "synthetic-root-fault-password-32"

type rootFaultFixture struct {
	engine *Engine
	server *httptest.Server
	root   string
}

func newRootFaultFixture(t *testing.T) *rootFaultFixture {
	t.Helper()
	root := t.TempDir()
	engine, err := New(Config{Username: "root-fault", Password: rootFaultPassword, ControlRoot: t.TempDir(), Shares: []Share{{Name: "dataset", Root: root}}})
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(engine)
	t.Cleanup(func() { server.Close(); _ = engine.Close() })
	return &rootFaultFixture{engine: engine, server: server, root: root}
}

func rootFaultRequest(t *testing.T, server *httptest.Server, method, path string, body io.Reader, headers map[string]string) (int, http.Header, []byte) {
	t.Helper()
	request, err := http.NewRequest(method, server.URL+path, body)
	if err != nil {
		t.Fatal(err)
	}
	request.SetBasicAuth("root-fault", rootFaultPassword)
	for key, value := range headers {
		request.Header.Set(key, value)
	}
	response, err := server.Client().Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	data, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	return response.StatusCode, response.Header, data
}

func expectRootFaultStatus(t *testing.T, server *httptest.Server, method, path string, want int) {
	t.Helper()
	status, _, body := rootFaultRequest(t, server, method, path, nil, nil)
	if status != want {
		t.Fatalf("%s %s = %d %s, want %d", method, path, status, body, want)
	}
}

func moveOpenExportForRootFault(t *testing.T, fixture *rootFaultFixture, moved string) {
	t.Helper()
	fixture.engine.mu.Lock()
	defer fixture.engine.mu.Unlock()
	x := fixture.engine.roots["dataset"]
	if runtime.GOOS == "windows" {
		// Windows denies renaming an open directory. Re-open the moved directory
		// as the test's filesystem seam to model Unix/Android's surviving inode.
		if err := x.root.Close(); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Rename(fixture.root, moved); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS == "windows" {
		var err error
		x.root, err = os.OpenRoot(moved)
		if err != nil {
			t.Fatal(err)
		}
	}
}

func TestShareRootStatChecksTheBackingDirectory(t *testing.T) {
	f := newRootFaultFixture(t)
	f.engine.mu.Lock()
	err := f.engine.roots["dataset"].root.Close()
	f.engine.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	f.engine.mu.Lock()
	_, statErr := (fileSystem{f.engine}).Stat(context.Background(), "/dataset/")
	f.engine.mu.Unlock()
	if statErr == nil {
		t.Fatal("share root Stat returned virtual success after its backing root was closed")
	}
}

type rootFaultBody struct {
	err       error
	delivered bool
}

func (b *rootFaultBody) Read(data []byte) (int, error) {
	if !b.delivered {
		b.delivered = true
		return copy(data, "partial-not-committed"), nil
	}
	return 0, b.err
}
func (*rootFaultBody) Close() error { return nil }

func TestPUTStorageExhaustionIs507WhileInterruptedBodyIs400(t *testing.T) {
	for _, fault := range []struct {
		name string
		err  error
		want int
	}{
		{"no-space", &os.PathError{Op: "write", Path: "private-storage-path-marker", Err: syscall.ENOSPC}, http.StatusInsufficientStorage},
		{"quota", &os.PathError{Op: "write", Path: "private-storage-path-marker", Err: syscall.EDQUOT}, http.StatusInsufficientStorage},
		{"truncated-body", io.ErrUnexpectedEOF, http.StatusBadRequest},
		{"cancelled-request", context.Canceled, http.StatusBadRequest},
	} {
		t.Run(fault.name, func(t *testing.T) {
			f := newRootFaultFixture(t)
			if err := os.WriteFile(filepath.Join(f.root, "committed"), []byte("old-committed-data"), 0600); err != nil {
				t.Fatal(err)
			}
			// The request-body boundary supplies the copy error without filling a
			// real disk or changing production filesystem dependencies.
			request := httptest.NewRequest("PUT", "/dataset/committed", &rootFaultBody{err: fault.err})
			request.SetBasicAuth("root-fault", rootFaultPassword)
			recorder := httptest.NewRecorder()
			f.engine.ServeHTTP(recorder, request)
			if recorder.Code != fault.want {
				t.Fatalf("PUT copy error returned %d, want %d", recorder.Code, fault.want)
			}
			if strings.Contains(recorder.Body.String(), "private-storage-path-marker") {
				t.Fatal("storage response exposed a physical path")
			}
			data, err := os.ReadFile(filepath.Join(f.root, "committed"))
			if err != nil || string(data) != "old-committed-data" {
				t.Fatalf("failed PUT changed committed data: %q %v", data, err)
			}
			entries, err := os.ReadDir(f.root)
			if err != nil {
				t.Fatal(err)
			}
			for _, entry := range entries {
				if strings.HasPrefix(entry.Name(), tempPrefix) {
					t.Fatal("failed PUT left an owned staging file")
				}
			}
		})
	}
}

func TestMovedOrReplacedShareRootFailsClosedOverHTTP(t *testing.T) {
	for _, replacement := range []string{"missing", "directory", "file"} {
		t.Run(replacement, func(t *testing.T) {
			f := newRootFaultFixture(t)
			if err := os.WriteFile(filepath.Join(f.root, "previous.txt"), []byte("old-root-private-content"), 0600); err != nil {
				t.Fatal(err)
			}
			expectRootFaultStatus(t, f.server, "GET", "/dataset/previous.txt", http.StatusOK)
			moved := f.root + "-moved"
			moveOpenExportForRootFault(t, f, moved)
			t.Cleanup(func() { f.server.Close(); _ = f.engine.Close(); _ = os.RemoveAll(moved) })
			switch replacement {
			case "directory":
				if err := os.Mkdir(f.root, 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(f.root, "replacement.txt"), []byte("replacement-root-content"), 0600); err != nil {
					t.Fatal(err)
				}
			case "file":
				if err := os.WriteFile(f.root, []byte("not-an-export-directory"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			for _, operation := range []struct {
				method  string
				path    string
				body    string
				headers map[string]string
			}{
				{"PROPFIND", "/dataset/", "", map[string]string{"Depth": "0"}},
				{"PROPFIND", "/dataset/", "", map[string]string{"Depth": "1"}},
				{"GET", "/dataset/previous.txt", "", nil},
				{"GET", "/dataset/replacement.txt", "", nil},
				{"PUT", "/dataset/created-after-root-loss", "not-published", nil},
				{"MKCOL", "/dataset/directory-after-root-loss", "", nil},
				{"DELETE", "/dataset/previous.txt", "", nil},
			} {
				status, _, body := rootFaultRequest(t, f.server, operation.method, operation.path, strings.NewReader(operation.body), operation.headers)
				if status >= 200 && status < 300 {
					t.Errorf("%s %s served an unavailable/replaced root: %d %s", operation.method, operation.path, status, body)
				}
				if strings.Contains(string(body), "old-root-private-content") || strings.Contains(string(body), "replacement-root-content") || strings.Contains(string(body), moved) {
					t.Errorf("root failure exposed data or a physical path: %s", body)
				}
			}
			data, err := os.ReadFile(filepath.Join(moved, "previous.txt"))
			if err != nil || string(data) != "old-root-private-content" {
				t.Fatalf("unavailable root's old inode was changed: %q %v", data, err)
			}
			for _, name := range []string{"created-after-root-loss", "directory-after-root-loss"} {
				if _, err := os.Stat(filepath.Join(moved, name)); !os.IsNotExist(err) {
					t.Errorf("mutation reached moved root: %s (%v)", name, err)
				}
			}
			// The failure belongs to this export, not the independent control root.
			expectRootFaultStatus(t, f.server, "MKCOL", "/.phonenas/still-available", http.StatusCreated)
		})
	}
}
