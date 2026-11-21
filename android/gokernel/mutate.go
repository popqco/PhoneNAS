package gokernel

import (
	"crypto/rand"
	"errors"
	"io"
	"net/http"
	"net/url"
	"os"
	"path"
	"strings"
	"syscall"
)

func fail(w http.ResponseWriter, status int) { http.Error(w, http.StatusText(status), status) }

// No error text contains a physical path or a credential.
func filesystemStatus(err error, missing int) int {
	switch {
	case errors.Is(err, os.ErrNotExist), errors.Is(err, syscall.ENOTDIR):
		return missing
	case errors.Is(err, os.ErrPermission), errors.Is(err, syscall.EROFS):
		return http.StatusForbidden
	case errors.Is(err, syscall.ENOSPC), errors.Is(err, syscall.EDQUOT):
		return http.StatusInsufficientStorage
	default:
		return http.StatusInternalServerError
	}
}

func parentOK(x *export, rel string) error {
	info, err := x.check(path.Dir(rel))
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return syscall.ENOTDIR
	}
	return nil
}

// MOVE uses Root.Rename directly. Never remove an existing destination first.
func (e *Engine) move(w http.ResponseWriter, r *http.Request, x *export, rel string) {
	u, err := url.Parse(r.Header.Get("Destination"))
	if err != nil || r.Header.Get("Destination") == "" || u.User != nil || u.Opaque != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || !canonicalURL(u) {
		fail(w, http.StatusBadRequest)
		return
	}
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	if u.Host != "" && !strings.EqualFold(u.Host, r.Host) || u.Scheme != "" && u.Scheme != scheme {
		fail(w, http.StatusBadGateway)
		return
	}
	dx, dest, err := (fileSystem{e}).resolve(u.Path)
	if errors.Is(err, os.ErrPermission) {
		fail(w, http.StatusForbidden)
		return
	}
	if err != nil {
		fail(w, http.StatusConflict)
		return
	}
	if dx == nil || dest == "." {
		fail(w, http.StatusForbidden)
		return
	}
	if x != dx {
		fail(w, http.StatusConflict)
		return
	}
	if dx.readOnly {
		fail(w, http.StatusForbidden)
		return
	}
	if rel == dest {
		fail(w, http.StatusForbidden)
		return
	}
	if strings.HasPrefix(dest, rel+"/") || strings.HasPrefix(rel, dest+"/") {
		fail(w, http.StatusConflict)
		return
	}
	if r.Header.Get("Depth") != "" && r.Header.Get("Depth") != "infinity" {
		fail(w, http.StatusBadRequest)
		return
	}
	overwrite := r.Header.Get("Overwrite")
	if overwrite != "" && overwrite != "T" && overwrite != "F" {
		fail(w, http.StatusBadRequest)
		return
	}
	if r.Header.Get("If") != "" || r.Header.Get("If-Match") != "" || r.Header.Get("If-None-Match") != "" {
		fail(w, http.StatusPreconditionFailed)
		return
	}
	srcInfo, err := x.check(rel)
	if err != nil {
		fail(w, filesystemStatus(err, http.StatusNotFound))
		return
	}
	if err := parentOK(x, dest); err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	dstInfo, err := x.check(dest)
	exists := err == nil
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	if exists {
		if overwrite == "F" {
			fail(w, http.StatusPreconditionFailed)
			return
		}
		// Replacing collections/type changes would require destructive deletion.
		if srcInfo.IsDir() || dstInfo.IsDir() || os.SameFile(srcInfo, dstInfo) {
			fail(w, http.StatusConflict)
			return
		}
	}
	if r.Context().Err() != nil {
		fail(w, http.StatusBadRequest)
		return
	}
	if err := x.root.Rename(rel, dest); err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	if exists {
		w.WriteHeader(http.StatusNoContent)
	} else {
		w.WriteHeader(http.StatusCreated)
	}
}

func remove(w http.ResponseWriter, r *http.Request, x *export, rel string) {
	if r.Header.Get("If") != "" || r.Header.Get("If-Match") != "" || r.Header.Get("If-None-Match") != "" {
		fail(w, http.StatusPreconditionFailed)
		return
	}
	if _, err := x.check(rel); err != nil {
		fail(w, filesystemStatus(err, http.StatusNotFound))
		return
	}
	if r.Context().Err() != nil {
		fail(w, http.StatusBadRequest)
		return
	}
	// Root.RemoveAll never follows directory symlinks encountered in the tree.
	if err := x.root.RemoveAll(rel); err != nil {
		fail(w, filesystemStatus(err, http.StatusNotFound))
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func mkcol(w http.ResponseWriter, r *http.Request, x *export, rel string) {
	if r.Header.Get("If") != "" {
		fail(w, http.StatusPreconditionFailed)
		return
	}
	if r.ContentLength != 0 {
		fail(w, http.StatusUnsupportedMediaType)
		return
	}
	if err := parentOK(x, rel); err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	if r.Context().Err() != nil {
		fail(w, http.StatusBadRequest)
		return
	}
	// No Stat -> MkdirAll: this single mkdir is the exclusive lock operation.
	err := x.root.Mkdir(rel, 0700)
	if errors.Is(err, os.ErrExist) {
		fail(w, http.StatusMethodNotAllowed)
		return
	}
	if err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	w.WriteHeader(http.StatusCreated)
}

// PUT is deliberately not delegated: upstream closes a file before checking
// copy errors. Publish only a complete, synced, closed same-root temporary file.
func (e *Engine) put(w http.ResponseWriter, r *http.Request, x *export, rel string) {
	info, err := x.check(rel)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	exists := err == nil
	if exists && !info.Mode().IsRegular() || r.URL.Path[len(r.URL.Path)-1] == '/' {
		fail(w, http.StatusConflict)
		return
	}
	if err := parentOK(x, rel); err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	// DAV locks are not implemented. Do not silently accept lock conditions.
	if r.Header.Get("If") != "" || r.Header.Get("If-Match") != "" || (r.Header.Get("If-None-Match") != "" && r.Header.Get("If-None-Match") != "*") {
		fail(w, http.StatusPreconditionFailed)
		return
	}
	if exists && r.Header.Get("If-None-Match") == "*" {
		fail(w, http.StatusPreconditionFailed)
		return
	}
	if r.ContentLength > e.maxUpload {
		fail(w, http.StatusRequestEntityTooLarge)
		return
	}
	tmp := path.Join(path.Dir(rel), tempPrefix+rand.Text())
	file, err := x.root.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	// The only temp we remove is the exact name successfully created above.
	defer func() { _ = file.Close(); _ = x.root.Remove(tmp) }()
	n, err := io.CopyBuffer(file, io.LimitReader(r.Body, e.maxUpload+1), make([]byte, 128<<10))
	if err != nil {
		status := http.StatusBadRequest // interrupted/truncated request body
		if errors.Is(err, syscall.ENOSPC) || errors.Is(err, syscall.EDQUOT) {
			status = http.StatusInsufficientStorage
		}
		fail(w, status)
		return
	}
	if n > e.maxUpload {
		fail(w, http.StatusRequestEntityTooLarge)
		return
	}
	if r.ContentLength >= 0 && n != r.ContentLength || r.Context().Err() != nil {
		fail(w, http.StatusBadRequest)
		return
	}
	if err := file.Sync(); err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	if err := file.Close(); err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	if r.Context().Err() != nil {
		fail(w, http.StatusBadRequest)
		return
	}
	if err := parentOK(x, rel); err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	current, err := x.check(rel)
	if exists {
		if err != nil || !current.Mode().IsRegular() || !os.SameFile(info, current) {
			fail(w, http.StatusConflict)
			return
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		fail(w, http.StatusConflict)
		return
	}
	if err := x.root.Rename(tmp, rel); err != nil {
		fail(w, filesystemStatus(err, http.StatusConflict))
		return
	}
	status := http.StatusCreated
	if exists {
		status = http.StatusNoContent
	}
	w.WriteHeader(status)
}
