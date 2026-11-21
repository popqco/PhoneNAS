// Package mobile exposes the native Android host's small lifecycle interface.
package mobile

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"phonenas.local/gokernel"
)

type configuration struct {
	Listen       string           `json:"listen"`
	RemoteListen string           `json:"remote_listen"`
	TLSDirectory string           `json:"tls_directory"`
	Username     string           `json:"username"`
	Password     string           `json:"password"`
	ControlRoot  string           `json:"control_root"`
	Shares       []gokernel.Share `json:"shares"`
}

// Server owns loopback HTTP and, when configured, Tailscale-only HTTPS.
// Both listeners share one confined engine and the same request accounting.
type Server struct {
	server        *http.Server
	remote        *http.Server
	identity      *tlsIdentity
	engine        *gokernel.Engine
	address       string
	remoteAddress string
	remoteAllowed atomic.Bool
	remoteContext context.Context
	cancelRemote  context.CancelFunc
	remoteOnce    sync.Once
	active        sync.RWMutex
	stopOnce      sync.Once
	stopping      atomic.Bool
	running       atomic.Bool
	requests      atomic.Int64
	received      atomic.Int64
	sent          atomic.Int64
	failed        atomic.Bool
}

// Start validates a debug configuration and opens the listener before returning.
func Start(configurationJSON string) (*Server, error) {
	return start(configurationJSON, net.Listen)
}

// start accepts a socket boundary so tests can run real TLS without changing
// interface addresses. The exported production entry point always uses net.Listen.
func start(configurationJSON string, listen func(string, string) (net.Listener, error)) (*Server, error) {
	if len(configurationJSON) > 65536 {
		return nil, errors.New("configuration exceeds the debug safety limit")
	}
	var config configuration
	decoder := json.NewDecoder(strings.NewReader(configurationJSON))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&config); err != nil {
		return nil, errors.New("invalid native engine configuration")
	}
	if decoder.Decode(new(any)) != io.EOF {
		return nil, errors.New("unexpected trailing configuration data")
	}
	host, portText, err := net.SplitHostPort(config.Listen)
	port, portErr := strconv.Atoi(portText)
	if err != nil || portErr != nil || port < 0 || port > 65535 || host != "127.0.0.1" {
		return nil, errors.New("debug engine requires an explicit IPv4 loopback listener")
	}
	if config.RemoteListen != "" {
		address, err := netip.ParseAddrPort(config.RemoteListen)
		if err != nil || !address.Addr().Is4() || !netip.MustParsePrefix("100.64.0.0/10").Contains(address.Addr()) || address.Port() <= 1024 {
			return nil, errors.New("remote listener requires a literal Tailscale IPv4 address and an unprivileged port")
		}
		if err := validateTLSDirectory(config); err != nil {
			return nil, err
		}
	}
	engine, err := gokernel.New(gokernel.Config{
		Username: config.Username, Password: config.Password,
		ControlRoot: config.ControlRoot, Shares: config.Shares,
	})
	if err != nil {
		return nil, err
	}
	var identity *tlsIdentity
	if config.RemoteListen != "" {
		identity, err = loadTLSIdentity(config)
		if err != nil {
			_ = engine.Close()
			return nil, err
		}
	}
	listener, err := listen("tcp4", config.Listen)
	if err != nil {
		engine.Close()
		return nil, errors.New("could not bind the isolated debug listener")
	}
	var remoteListener net.Listener
	if config.RemoteListen != "" {
		remoteListener, err = listen("tcp4", config.RemoteListen)
		if err != nil {
			_ = listener.Close()
			_ = engine.Close()
			return nil, errors.New("could not bind the private remote listener")
		}
	}
	s := &Server{engine: engine, identity: identity, address: listener.Addr().String(), remoteAddress: config.RemoteListen}
	limit := make(chan struct{}, 16)
	s.server = &http.Server{
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       120 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    16384,
		ErrorLog:          log.New(io.Discard, "", 0),
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			s.active.RLock()
			defer s.active.RUnlock()
			if s.stopping.Load() {
				http.Error(w, "stopping", http.StatusServiceUnavailable)
				return
			}
			select {
			case limit <- struct{}{}:
				defer func() { <-limit }()
			default:
				http.Error(w, "busy", http.StatusServiceUnavailable)
				return
			}
			s.requests.Add(1)
			r.Body = &countedBody{ReadCloser: r.Body, count: &s.received}
			response := &countedResponse{ResponseWriter: w, count: &s.sent, request: r}
			s.engine.ServeHTTP(response, r)
			// Engine error responses are small and still buffered by net/http.
			// Drain only after ServeHTTP releases the engine's filesystem mutex,
			// and before net/http flushes and closes the rejected connection.
			response.finishRejectedBody()
		}),
	}
	if remoteListener != nil {
		s.remoteContext, s.cancelRemote = context.WithCancel(context.Background())
		s.remoteAllowed.Store(true)
		s.remote = &http.Server{
			ReadHeaderTimeout: 10 * time.Second,
			ReadTimeout:       120 * time.Second,
			WriteTimeout:      120 * time.Second,
			IdleTimeout:       30 * time.Second,
			MaxHeaderBytes:    16384,
			ErrorLog:          log.New(io.Discard, "", 0),
			BaseContext:       func(net.Listener) context.Context { return s.remoteContext },
			Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if !s.remoteAllowed.Load() {
					http.Error(w, "remote access is disabled", http.StatusServiceUnavailable)
					return
				}
				peer, err := netip.ParseAddrPort(r.RemoteAddr)
				if err != nil || !peer.Addr().Is4() || peer.Port() == 0 || !netip.MustParsePrefix("100.64.0.0/10").Contains(peer.Addr()) {
					http.Error(w, "remote source is not permitted", http.StatusForbidden)
					return
				}
				s.server.Handler.ServeHTTP(w, r)
			}),
		}
	}
	s.running.Store(true)
	serve := func(server *http.Server, socket net.Listener) {
		_ = server.Serve(socket)
		if !s.stopping.Load() && (server != s.remote || s.remoteAllowed.Load()) {
			s.failed.Store(true)
			s.running.Store(false)
			s.Stop()
		}
	}
	go serve(s.server, listener)
	if remoteListener != nil {
		go serve(s.remote, tls.NewListener(remoteListener, &tls.Config{
			MinVersion: tls.VersionTLS12, Certificates: []tls.Certificate{identity.certificate},
		}))
	}
	return s, nil
}

func (s *Server) Address() string { return s.address }

// Certificate returns only the public PEM certificate, or empty for loopback-only.
func (s *Server) Certificate() string {
	if s.identity == nil {
		return ""
	}
	return s.identity.publicPEM
}

// Status never contains credentials, user paths, request bodies or lock tokens.
func (s *Server) Status() string {
	lastError := ""
	if s.failed.Load() {
		lastError = "listener stopped unexpectedly"
	}
	fingerprint := ""
	if s.identity != nil {
		fingerprint = s.identity.fingerprint
	}
	remoteAddress := ""
	if s.remoteAllowed.Load() && !s.stopping.Load() {
		remoteAddress = s.remoteAddress
	}
	result, _ := json.Marshal(struct {
		Running           bool   `json:"running"`
		Address           string `json:"address"`
		RemoteAddress     string `json:"remote_address"`
		CertificateSHA256 string `json:"certificate_sha256"`
		Requests          int64  `json:"requests"`
		BytesReceived     int64  `json:"bytes_received"`
		BytesSent         int64  `json:"bytes_sent"`
		LastError         string `json:"last_error"`
	}{s.running.Load(), s.address, remoteAddress, fingerprint, s.requests.Load(), s.received.Load(), s.sent.Load(), lastError})
	return string(result)
}

// RevokeRemote immediately cancels and closes remote access without waiting for
// graceful shutdown. It never reopens access, rotates identity, or changes local
// HTTP, the engine, committed files, or dataset locks. It is safe with Stop.
func (s *Server) RevokeRemote() {
	s.remoteAllowed.Store(false)
	s.remoteOnce.Do(func() {
		if s.cancelRemote != nil {
			s.cancelRemote()
		}
		if s.remote != nil {
			_ = s.remote.Close()
		}
	})
}

// Stop closes both listeners and the engine; it never removes data or locks.
func (s *Server) Stop() {
	s.stopOnce.Do(func() {
		s.stopping.Store(true)
		s.running.Store(false)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var wait sync.WaitGroup
		for _, server := range []*http.Server{s.server, s.remote} {
			if server == nil {
				continue
			}
			wait.Add(1)
			go func(server *http.Server) {
				defer wait.Done()
				if server.Shutdown(ctx) != nil {
					_ = server.Close()
				}
			}(server)
		}
		wait.Wait()
		s.active.Lock()
		defer s.active.Unlock()
		s.engine.Close()
		s.running.Store(false)
	})
}

type countedBody struct {
	io.ReadCloser
	count *atomic.Int64
}

func (b *countedBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	b.count.Add(int64(n))
	return n, err
}

type countedResponse struct {
	http.ResponseWriter
	count   *atomic.Int64
	request *http.Request
	status  int
}

func (w *countedResponse) WriteHeader(status int) {
	if w.status == 0 && status >= 200 {
		w.status = status
	}
	w.ResponseWriter.WriteHeader(status)
}

func (w *countedResponse) Write(p []byte) (int, error) {
	if w.status == 0 {
		w.status = http.StatusOK
	}
	n, err := w.ResponseWriter.Write(p)
	w.count.Add(int64(n))
	return n, err
}

func (w *countedResponse) finishRejectedBody() {
	const maxRejectedBody = 64 << 10
	const drainTimeout = 250 * time.Millisecond
	r := w.request
	if w.status < 400 || r == nil || r.Body == nil || r.ContentLength == 0 {
		return
	}
	controller := http.NewResponseController(w.ResponseWriter)
	// Disable net/http's own 256KiB post-handler drain. Only this explicitly
	// bounded read may wait for a rejected body; normal PUT handling is unchanged.
	_ = controller.EnableFullDuplex()
	if r.ContentLength < 0 || r.ContentLength > maxRejectedBody || len(r.TransferEncoding) != 0 ||
		r.Header.Get("Expect") != "" || r.Context().Err() != nil {
		w.Header().Set("Connection", "close")
		_ = controller.SetReadDeadline(time.Now())
		return
	}
	deadline := time.Now().Add(drainTimeout)
	if contextDeadline, ok := r.Context().Deadline(); ok && contextDeadline.Before(deadline) {
		deadline = contextDeadline
	}
	if err := controller.SetReadDeadline(deadline); err != nil {
		// Without transport deadlines never turn a rejection into a slow read.
		w.Header().Set("Connection", "close")
		return
	}
	_, err := io.Copy(io.Discard, io.LimitReader(r.Body, r.ContentLength))
	if err != nil {
		w.Header().Set("Connection", "close")
		_ = controller.SetReadDeadline(time.Now())
		return
	}
	// net/http supplies the next request's deadline. No old deadline may leak
	// into a reused connection after the complete, small body has been consumed.
	_ = controller.SetReadDeadline(time.Time{})
}

func (w *countedResponse) Unwrap() http.ResponseWriter { return w.ResponseWriter }
