// Package gokernel serves explicitly configured, confined WebDAV exports.
package gokernel

import (
	"bytes"
	"crypto/sha256"
	"crypto/subtle"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"unicode/utf8"

	"golang.org/x/net/webdav"
)

type Share struct {
	Name     string `json:"name"`
	Root     string `json:"root"`
	ReadOnly bool   `json:"read_only"`
}

type Config struct {
	Username       string  `json:"username"`
	Password       string  `json:"password"`
	ControlRoot    string  `json:"control_root"`
	Shares         []Share `json:"shares"`
	MaxUploadBytes int64   `json:"max_upload_bytes"`
}

type export struct {
	root     *os.Root
	path     string
	identity os.FileInfo
	readOnly bool
}

type Engine struct {
	mu        sync.Mutex
	roots     map[string]*export
	user      [32]byte
	password  [32]byte
	dav       *webdav.Handler
	closed    bool
	maxUpload int64
}

func New(c Config) (*Engine, error) {
	if c.Username == "" || len(c.Password) < 20 {
		return nil, errors.New("username and password of at least 20 bytes required")
	}
	if c.MaxUploadBytes == 0 {
		c.MaxUploadBytes = 64 << 30
	}
	if c.MaxUploadBytes < 1 || c.MaxUploadBytes == 1<<63-1 {
		return nil, errors.New("invalid upload limit")
	}
	e := &Engine{roots: make(map[string]*export), user: sha256.Sum256([]byte(c.Username)), password: sha256.Sum256([]byte(c.Password)), maxUpload: c.MaxUploadBytes}
	shares := append([]Share{{Name: controlName, Root: c.ControlRoot}}, c.Shares...)
	var paths []string
	var infos []os.FileInfo
	for i, s := range shares {
		if !validName(s.Name) || (i > 0 && reserved(s.Name)) || s.Root == "" || e.roots[s.Name] != nil {
			_ = e.Close()
			return nil, errors.New("invalid or duplicate export")
		}
		p, info, err := canonicalRoot(s.Root)
		if err != nil {
			_ = e.Close()
			return nil, err
		}
		for j, other := range paths {
			if os.SameFile(info, infos[j]) || overlaps(p, other) {
				_ = e.Close()
				return nil, errors.New("configured roots must not overlap")
			}
		}
		paths, infos = append(paths, p), append(infos, info)
		r, err := os.OpenRoot(p)
		if err != nil {
			_ = e.Close()
			return nil, errors.New("cannot open configured root")
		}
		e.roots[s.Name] = &export{root: r, path: p, identity: info, readOnly: s.ReadOnly}
	}
	e.dav = &webdav.Handler{FileSystem: fileSystem{e}, LockSystem: webdav.NewMemLS()}
	return e, nil
}

func validName(s string) bool {
	return utf8.ValidString(s) && s != "" && s != "." && s != ".." && !strings.ContainsAny(s, "/\\\x00")
}

func canonicalRoot(p string) (string, os.FileInfo, error) {
	if !filepath.IsAbs(p) {
		return "", nil, errors.New("root must be absolute")
	}
	p = filepath.Clean(p)
	// Reject symlinks anywhere in a configured path, not just its final name.
	for part := p; ; part = filepath.Dir(part) {
		fi, err := os.Lstat(part)
		if err != nil || fi.Mode()&os.ModeSymlink != 0 || !fi.IsDir() {
			return "", nil, errors.New("root must be an existing nonsymlink directory")
		}
		if filepath.Dir(part) == part {
			break
		}
	}
	canonical, err := filepath.EvalSymlinks(p)
	if err != nil {
		return "", nil, errors.New("cannot canonicalize configured root")
	}
	info, err := os.Stat(canonical)
	if err != nil {
		return "", nil, errors.New("cannot inspect configured root")
	}
	return canonical, info, nil
}

func overlaps(a, b string) bool {
	if runtime.GOOS == "windows" {
		a, b = strings.ToLower(a), strings.ToLower(b)
	}
	sep := string(filepath.Separator)
	return a == b || strings.HasPrefix(a, strings.TrimSuffix(b, sep)+sep) || strings.HasPrefix(b, strings.TrimSuffix(a, sep)+sep)
}

// Close releases root descriptors. Shut down the HTTP server before calling it.
func (e *Engine) Close() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.closed = true
	var errs []error
	for _, x := range e.roots {
		errs = append(errs, x.root.Close())
	}
	e.roots = nil
	return errors.Join(errs...)
}

func (e *Engine) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	u, p, ok := r.BasicAuth()
	uh, ph := sha256.Sum256([]byte(u)), sha256.Sum256([]byte(p))
	valid := subtle.ConstantTimeCompare(uh[:], e.user[:]) & subtle.ConstantTimeCompare(ph[:], e.password[:])
	if !ok || valid != 1 {
		w.Header().Set("WWW-Authenticate", `Basic realm="PhoneNAS", charset="UTF-8"`)
		http.Error(w, "authentication required", http.StatusUnauthorized)
		return
	}
	if r.Method != "PUT" {
		const metadataLimit = 64 << 10
		if r.ContentLength > metadataLimit {
			fail(w, http.StatusRequestEntityTooLarge)
			return
		}
		body, err := io.ReadAll(io.LimitReader(r.Body, metadataLimit+1))
		if err != nil {
			fail(w, http.StatusBadRequest)
			return
		}
		if len(body) > metadataLimit {
			fail(w, http.StatusRequestEntityTooLarge)
			return
		}
		r.Body.Close()
		r.Body = io.NopCloser(bytes.NewReader(body))
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if r.Context().Err() != nil {
		fail(w, http.StatusBadRequest)
		return
	}
	if e.closed {
		http.Error(w, "engine closed", http.StatusServiceUnavailable)
		return
	}
	if !canonicalURL(r.URL) {
		http.Error(w, "noncanonical path", http.StatusBadRequest)
		return
	}
	x, rel, err := (fileSystem{e}).resolve(r.URL.Path)
	if errors.Is(err, os.ErrPermission) {
		http.Error(w, "reserved path", http.StatusForbidden)
		return
	}
	if x != nil {
		if _, err := x.check(rel); errors.Is(err, os.ErrPermission) {
			http.Error(w, "path is not exportable", http.StatusForbidden)
			return
		}
	}
	if r.Method == "PROPFIND" && r.Header.Get("Depth") != "0" && r.Header.Get("Depth") != "1" {
		http.Error(w, "finite Depth 0 or 1 required", http.StatusForbidden)
		return
	}
	if r.Method != "GET" && r.Method != "HEAD" && r.Method != "PROPFIND" && r.Method != "OPTIONS" {
		if x != nil && x.readOnly {
			http.Error(w, "read only export", http.StatusForbidden)
			return
		}
		if x == nil || rel == "." {
			status := http.StatusForbidden
			if r.Method == "MKCOL" {
				status = http.StatusMethodNotAllowed
			}
			http.Error(w, "export roots cannot be changed", status)
			return
		}
	}
	switch r.Method {
	case "PUT":
		e.put(w, r, x, rel)
	case "MKCOL":
		mkcol(w, r, x, rel)
	case "MOVE":
		e.move(w, r, x, rel)
	case "DELETE":
		remove(w, r, x, rel)
	case "OPTIONS":
		allow := "OPTIONS, GET, HEAD, PROPFIND"
		if x != nil && !x.readOnly {
			allow += ", PUT, MKCOL, MOVE, DELETE"
		}
		w.Header().Set("Allow", allow)
		w.Header().Set("DAV", "1")
		w.Header().Set("MS-Author-Via", "DAV")
		w.WriteHeader(http.StatusOK)
	case "COPY":
		fail(w, http.StatusNotImplemented)
	case "GET", "HEAD", "PROPFIND":
		e.dav.ServeHTTP(w, r)
	default:
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}
