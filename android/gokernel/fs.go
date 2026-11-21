package gokernel

import (
	"context"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/net/webdav"
)

const controlName = ".phonenas"
const tempPrefix = ".phonenas-temp-"

func reserved(s string) bool {
	s = strings.ToLower(s)
	return s == controlName || strings.HasPrefix(s, tempPrefix)
}

// URL.Path is already decoded by net/http. Never unescape it a second time.
func canonicalURL(u *url.URL) bool {
	p := u.Path
	if !utf8.ValidString(p) || !strings.HasPrefix(p, "/") || strings.ContainsAny(p, "\x00\\") {
		return false
	}
	raw := u.EscapedPath()
	if u.RawPath != "" {
		decoded, err := url.PathUnescape(u.RawPath)
		if err != nil || decoded != p {
			return false
		}
		raw = u.RawPath
	}
	raw = strings.ToLower(raw)
	if strings.Contains(raw, "%2f") || strings.Contains(raw, "%5c") {
		return false
	}
	if p == "/" {
		return true
	}
	for _, s := range strings.Split(strings.TrimSuffix(p[1:], "/"), "/") {
		if s == "" || s == "." || s == ".." {
			return false
		}
	}
	return true
}

type fileSystem struct{ e *Engine }

func (f fileSystem) resolve(name string) (*export, string, error) {
	if !canonicalURL(&url.URL{Path: name}) {
		return nil, "", os.ErrInvalid
	}
	parts := strings.Split(strings.TrimSuffix(strings.TrimPrefix(name, "/"), "/"), "/")
	if name == "/" {
		return nil, ".", nil
	}
	x := f.e.roots[parts[0]]
	if x == nil {
		return nil, "", os.ErrNotExist
	}
	for _, s := range parts[1:] {
		if reserved(s) {
			return nil, "", os.ErrPermission
		}
	}
	if len(parts) == 1 {
		return x, ".", nil
	}
	return x, strings.Join(parts[1:], "/"), nil
}

// A retained os.Root may still access an old inode after an unmount or rename.
// Require the configured path (and every ancestor) to remain a nonsymlink
// directory and the final directory to match the identity opened at startup.
func (x *export) checkRoot() error {
	for part := x.path; ; part = filepath.Dir(part) {
		info, err := os.Lstat(part)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || (part == x.path && !os.SameFile(info, x.identity)) {
			return os.ErrPermission
		}
		if filepath.Dir(part) == part {
			break
		}
	}
	return nil
}

// Lstat every existing component: symlink aliases (even internal ones) and
// special files are not exports. os.Root independently enforces confinement
// when a local process races these checks. Local filesystem writers are trusted.
func (x *export) check(rel string) (os.FileInfo, error) {
	if err := x.checkRoot(); err != nil {
		return nil, err
	}
	prefix := ""
	for _, part := range strings.Split(rel, "/") {
		if prefix != "" {
			prefix += "/"
		}
		prefix += part
		info, err := x.root.Lstat(prefix)
		if err != nil {
			return nil, err
		}
		if !info.IsDir() && !info.Mode().IsRegular() {
			return nil, os.ErrPermission
		}
		if prefix == rel {
			return info, nil
		}
	}
	return nil, os.ErrInvalid
}

func (f fileSystem) OpenFile(_ context.Context, name string, flag int, perm os.FileMode) (webdav.File, error) {
	if flag != os.O_RDONLY {
		return nil, os.ErrPermission
	}
	x, rel, err := f.resolve(name)
	if err != nil {
		return nil, err
	}
	if x == nil {
		entries := make([]os.FileInfo, 0, len(f.e.roots))
		for name := range f.e.roots {
			entries = append(entries, dirInfo(name))
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		return &virtualDir{entries: entries}, nil
	}
	if _, err := x.check(rel); err != nil {
		return nil, err
	}
	file, err := x.root.OpenFile(rel, flag, perm)
	if err != nil {
		return nil, err
	}
	return &davFile{File: file, root: rel == ".", name: strings.Trim(name, "/")}, nil
}

func (f fileSystem) Stat(_ context.Context, name string) (os.FileInfo, error) {
	x, rel, err := f.resolve(name)
	if err != nil {
		return nil, err
	}
	if x == nil {
		return dirInfo(strings.Trim(name, "/")), nil
	}
	info, err := x.check(rel)
	if err != nil {
		return nil, err
	}
	if rel == "." {
		return dirInfo(strings.Trim(name, "/")), nil
	}
	return info, nil
}

func (f fileSystem) Mkdir(context.Context, string, os.FileMode) error { return os.ErrPermission }
func (f fileSystem) RemoveAll(context.Context, string) error          { return os.ErrPermission }
func (f fileSystem) Rename(context.Context, string, string) error     { return os.ErrPermission }

type dirInfo string

func (i dirInfo) Name() string     { return string(i) }
func (dirInfo) Size() int64        { return 0 }
func (dirInfo) Mode() os.FileMode  { return os.ModeDir | 0700 }
func (dirInfo) ModTime() time.Time { return time.Unix(0, 0).UTC() }
func (dirInfo) IsDir() bool        { return true }
func (dirInfo) Sys() any           { return nil }

type virtualDir struct {
	entries []os.FileInfo
	offset  int
}

func (*virtualDir) Close() error                   { return nil }
func (*virtualDir) Read([]byte) (int, error)       { return 0, os.ErrInvalid }
func (*virtualDir) Write([]byte) (int, error)      { return 0, os.ErrPermission }
func (*virtualDir) Seek(int64, int) (int64, error) { return 0, os.ErrInvalid }
func (*virtualDir) Stat() (os.FileInfo, error)     { return dirInfo("/"), nil }
func (d *virtualDir) Readdir(n int) ([]os.FileInfo, error) {
	if n > 0 && d.offset == len(d.entries) {
		return nil, io.EOF
	}
	end := len(d.entries)
	if n > 0 && end-d.offset > n {
		end = d.offset + n
	}
	entries := d.entries[d.offset:end]
	d.offset = end
	return entries, nil
}

type davFile struct {
	*os.File
	root bool
	name string
}

func (f *davFile) Stat() (os.FileInfo, error) {
	if f.root {
		return dirInfo(f.name), nil
	}
	return f.File.Stat()
}

func (f *davFile) Readdir(n int) ([]os.FileInfo, error) {
	var out []os.FileInfo
	for {
		batch := n
		if n > 0 {
			batch -= len(out)
		}
		entries, err := f.File.Readdir(batch)
		for _, info := range entries {
			if !reserved(info.Name()) && (info.IsDir() || info.Mode().IsRegular()) {
				out = append(out, info)
			}
		}
		if err != nil || n <= 0 || len(out) == n {
			return out, err
		}
	}
}
