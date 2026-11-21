package gokernel_test

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestPROPFINDMetadataBodyIsBounded(t *testing.T) {
	f := newFixture(t)
	body := `<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>` +
		strings.Repeat(`<d:displayname/>`, 6000) + `</d:prop></d:propfind>`
	for _, input := range []io.Reader{strings.NewReader(body), io.LimitReader(strings.NewReader(body), int64(len(body)))} {
		status, _, _ := request(t, f.server, "PROPFIND", "/dataset/", input, map[string]string{"Depth": "1"})
		if status != http.StatusRequestEntityTooLarge {
			t.Fatalf("oversized metadata must return 413, got %d", status)
		}
	}
}

func TestCancelledMutationCannotCreateDirectory(t *testing.T) {
	f := newFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req := httptest.NewRequest("MKCOL", "/dataset/cancelled", nil).WithContext(ctx)
	req.SetBasicAuth("owner", password)
	response := httptest.NewRecorder()
	f.engine.ServeHTTP(response, req)
	if response.Code >= 200 && response.Code < 300 {
		t.Fatalf("cancelled MKCOL returned success: %d", response.Code)
	}
	expectStatus(t, f.server, "GET", "/dataset/cancelled", 404)
}
