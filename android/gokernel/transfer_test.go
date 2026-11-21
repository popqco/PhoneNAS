package gokernel_test

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func startSlowUpload(t *testing.T, f *fixture, name, first, last string, createOnly bool) (*io.PipeWriter, <-chan int) {
	t.Helper()
	reader, writer := io.Pipe()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	t.Cleanup(cancel)
	t.Cleanup(func() { _ = writer.Close() })
	req, err := http.NewRequestWithContext(ctx, "PUT", f.server.URL+"/dataset/"+name, reader)
	if err != nil {
		t.Fatal(err)
	}
	req.SetBasicAuth("owner", password)
	req.ContentLength = int64(len(first) + len(last))
	if createOnly {
		req.Header.Set("If-None-Match", "*")
	}
	result := make(chan int, 1)
	go func() {
		res, err := f.server.Client().Do(req)
		if err != nil {
			result <- 0
			return
		}
		_, _ = io.Copy(io.Discard, res.Body)
		_ = res.Body.Close()
		result <- res.StatusCode
	}()
	if _, err := io.WriteString(writer, first); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		entries, err := os.ReadDir(f.root)
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range entries {
			if strings.HasPrefix(entry.Name(), ".phonenas-temp-") {
				return writer, result
			}
		}
		if !time.Now().Before(deadline) {
			t.Fatal("PUT did not enter staging")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func finishSlowUpload(t *testing.T, f *fixture, writer *io.PipeWriter, result <-chan int, last string, want int) {
	t.Helper()
	if _, err := io.WriteString(writer, last); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	if got := <-result; got != want {
		t.Fatalf("PUT = %d, want %d", got, want)
	}
	entries, err := os.ReadDir(f.root)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".phonenas-temp-") {
			t.Fatal("completed PUT left staging data")
		}
	}
}

func TestSlowPUTLeavesCommittedFilesAndListingAvailable(t *testing.T) {
	f := newFixture(t)
	const original = "original bytes"
	if err := os.WriteFile(filepath.Join(f.root, "existing.txt"), []byte(original), 0600); err != nil {
		t.Fatal(err)
	}
	w, uploaded := startSlowUpload(t, f, "existing.txt", "new ", "complete bytes", false)
	for _, check := range []struct {
		method, path string
		want         int
	}{
		{"GET", "/dataset/existing.txt", http.StatusOK},
		{"PROPFIND", "/dataset/", http.StatusMultiStatus},
	} {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		req, err := http.NewRequestWithContext(ctx, check.method, f.server.URL+check.path, nil)
		if err != nil {
			cancel()
			t.Fatal(err)
		}
		req.SetBasicAuth("owner", password)
		req.Header.Set("Depth", "1")
		started := time.Now()
		res, err := f.server.Client().Do(req)
		if err != nil {
			cancel()
			t.Fatalf("%s stalled behind an incomplete PUT after %s: %v", check.method, time.Since(started), err)
		}
		body, err := io.ReadAll(res.Body)
		_ = res.Body.Close()
		cancel()
		if err != nil || res.StatusCode != check.want {
			t.Fatalf("%s while PUT paused = %d, %v", check.method, res.StatusCode, err)
		}
		t.Logf("%s completed while PUT was paused in %s", check.method, time.Since(started))
		if check.method == "GET" && string(body) != original {
			t.Fatal("GET did not see the complete old file")
		}
		if check.method == "PROPFIND" && strings.Contains(string(body), ".phonenas-temp-") {
			t.Fatal("listing included staging data")
		}
	}
	finishSlowUpload(t, f, w, uploaded, "complete bytes", http.StatusNoContent)
	if got := expectStatus(t, f.server, "GET", "/dataset/existing.txt", http.StatusOK); string(got) != "new complete bytes" {
		t.Fatal("committed bytes differ")
	}
}

func TestExpiredPUTCannotPublishAfterWaitingForAReader(t *testing.T) {
	f := newFixture(t)
	if err := os.WriteFile(filepath.Join(f.root, "old"), []byte("existing"), 0600); err != nil {
		t.Fatal(err)
	}
	entered, release, readDone := make(chan struct{}), make(chan struct{}), make(chan struct{})
	reader := &pausedResponse{ResponseRecorder: httptest.NewRecorder(), entered: entered, resume: release}
	get := httptest.NewRequest("GET", "/dataset/old", nil)
	get.SetBasicAuth("owner", password)
	go func() { defer close(readDone); f.engine.ServeHTTP(reader, get) }()
	unblock := sync.OnceFunc(func() { close(release); <-readDone })
	defer unblock()
	select {
	case <-entered:
	case <-time.After(2 * time.Second):
		t.Fatal("reader did not start writing")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	put := httptest.NewRequest("PUT", "/dataset/new", strings.NewReader("complete body")).WithContext(ctx)
	put.SetBasicAuth("owner", password)
	writer := httptest.NewRecorder()
	putDone := make(chan struct{})
	go func() { defer close(putDone); f.engine.ServeHTTP(writer, put) }()
	<-ctx.Done()
	unblock()
	select {
	case <-putDone:
	case <-time.After(2 * time.Second):
		t.Fatal("expired upload did not return")
	}
	if writer.Code != http.StatusBadRequest {
		t.Fatalf("expired PUT returned %d", writer.Code)
	}
	if _, err := os.Stat(filepath.Join(f.root, "new")); !os.IsNotExist(err) {
		t.Fatal("expired PUT published a file")
	}
}

type pausedResponse struct {
	*httptest.ResponseRecorder
	entered, resume chan struct{}
}

func (w *pausedResponse) Write(p []byte) (int, error) {
	close(w.entered)
	<-w.resume
	return w.ResponseRecorder.Write(p)
}

func TestConditionalPUTPreservesNameCreatedByLocalWriter(t *testing.T) {
	f := newFixture(t)
	w, uploaded := startSlowUpload(t, f, "collision.txt", "incoming ", "bytes", true)
	file, err := os.OpenFile(filepath.Join(f.root, "collision.txt"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	_, writeErr := io.WriteString(file, "independent writer")
	closeErr := file.Close()
	if writeErr != nil || closeErr != nil {
		t.Fatalf("independent writer failed: %v %v", writeErr, closeErr)
	}
	finishSlowUpload(t, f, w, uploaded, "bytes", http.StatusPreconditionFailed)
	if got := expectStatus(t, f.server, "GET", "/dataset/collision.txt", http.StatusOK); string(got) != "independent writer" {
		t.Fatal("conditional PUT changed the independent writer's bytes")
	}
}
