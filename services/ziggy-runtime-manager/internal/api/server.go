// Package api exposes the manager's deliberately small Unix-socket protocol.
package api

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-runtime-manager/runtime"
)

type Request struct {
	Operation   string `json:"operation"`
	WorkspaceID string `json:"workspace_id"`
	Generation  uint64 `json:"generation"`
}

type Response struct {
	Status *runtime.Status `json:"status,omitempty"`
	Error  string          `json:"error,omitempty"`
}

type Server struct {
	Driver          runtime.Driver
	Logger          *log.Logger
	MaxConcurrent   int
	MaxRequestBytes int64
	ReadTimeout     time.Duration
	WriteTimeout    time.Duration
	DriverTimeout   time.Duration
	SocketGroupID   int
	SocketGroupSet  bool
	DestructiveUID  int
	DestructiveSet  bool
}

const (
	defaultMaxConcurrent   = 16
	defaultMaxRequestBytes = 4096
	defaultReadTimeout     = 5 * time.Second
	defaultWriteTimeout    = 5 * time.Second
	defaultDriverTimeout   = 30 * time.Second
	maximumConcurrent      = 256
	maximumRequestBytes    = 64 << 10
	maximumTimeout         = time.Minute
)

type limits struct {
	maxConcurrent   int
	maxRequestBytes int64
	readTimeout     time.Duration
	writeTimeout    time.Duration
	driverTimeout   time.Duration
}

type peerIdentity struct {
	UID int
	GID int
	PID int
}

func (s Server) limits() limits {
	result := limits{defaultMaxConcurrent, defaultMaxRequestBytes, defaultReadTimeout, defaultWriteTimeout, defaultDriverTimeout}
	if s.MaxConcurrent > 0 && s.MaxConcurrent <= maximumConcurrent {
		result.maxConcurrent = s.MaxConcurrent
	}
	if s.MaxRequestBytes > 0 && s.MaxRequestBytes <= maximumRequestBytes {
		result.maxRequestBytes = s.MaxRequestBytes
	}
	if s.ReadTimeout > 0 && s.ReadTimeout <= maximumTimeout {
		result.readTimeout = s.ReadTimeout
	}
	if s.WriteTimeout > 0 && s.WriteTimeout <= maximumTimeout {
		result.writeTimeout = s.WriteTimeout
	}
	if s.DriverTimeout > 0 && s.DriverTimeout <= maximumTimeout {
		result.driverTimeout = s.DriverTimeout
	}
	return result
}

func (s Server) ListenAndServe(ctx context.Context, socket string) error {
	if s.Driver == nil {
		return errors.New("runtime driver is required")
	}
	if s.SocketGroupSet && s.SocketGroupID < 0 {
		return errors.New("socket group ID must not be negative")
	}
	if s.DestructiveSet && s.DestructiveUID < 0 {
		return errors.New("destructive peer UID must not be negative")
	}
	if err := s.prepareSocketDirectory(filepath.Dir(socket)); err != nil {
		return err
	}
	lock, err := acquireSocketLock(socket + ".lock")
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := removeStaleSocket(socket); err != nil {
		return err
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	defer os.Remove(socket)
	if s.SocketGroupSet {
		if err := os.Chown(socket, -1, s.SocketGroupID); err != nil {
			return err
		}
	}
	socketMode := os.FileMode(0o600)
	if s.SocketGroupSet {
		socketMode = 0o660
	}
	if err := os.Chmod(socket, socketMode); err != nil {
		listener.Close()
		return err
	}
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()
	limits := s.limits()
	slots := make(chan struct{}, limits.maxConcurrent)
	for {
		connection, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return err
		}
		peer, err := socketPeerIdentity(connection)
		if err != nil || !s.authorizePeer(peer) {
			s.writeResponse(connection, limits, Response{Error: "unauthorized_peer"})
			_ = connection.Close()
			continue
		}
		select {
		case slots <- struct{}{}:
			go func() {
				defer func() { <-slots }()
				s.serveConnection(ctx, connection, limits, peer)
			}()
		default:
			s.writeResponse(connection, limits, Response{Error: "server_busy"})
			_ = connection.Close()
		}
	}
}

func (s Server) serveConnection(ctx context.Context, connection net.Conn, limits limits, peer peerIdentity) {
	defer connection.Close()
	if err := connection.SetReadDeadline(time.Now().Add(limits.readTimeout)); err != nil {
		return
	}
	line, err := bufio.NewReaderSize(io.LimitReader(connection, limits.maxRequestBytes+1), int(limits.maxRequestBytes+1)).ReadString('\n')
	if len(line) > int(limits.maxRequestBytes) || errors.Is(err, bufio.ErrBufferFull) {
		s.writeResponse(connection, limits, Response{Error: "request_too_large"})
		return
	}
	if err != nil && !errors.Is(err, io.EOF) {
		s.writeResponse(connection, limits, Response{Error: requestErrorCode(err)})
		return
	}
	decoder := json.NewDecoder(strings.NewReader(line))
	decoder.DisallowUnknownFields()
	var request Request
	if err := decoder.Decode(&request); err != nil {
		s.writeResponse(connection, limits, Response{Error: requestErrorCode(err)})
		return
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		s.writeResponse(connection, limits, Response{Error: requestErrorCode(err)})
		return
	}
	if err := connection.SetReadDeadline(time.Time{}); err != nil {
		return
	}
	if request.Operation == "delete_tenant_data" && !s.authorizeDestructivePeer(peer) {
		s.log(request, "forbidden")
		s.writeResponse(connection, limits, Response{Error: "destructive_operation_forbidden"})
		return
	}
	driverCtx, cancel := context.WithTimeout(ctx, limits.driverTimeout)
	defer cancel()
	status, err := s.dispatch(driverCtx, request)
	if err != nil {
		s.log(request, "failed")
		s.writeResponse(connection, limits, Response{Error: errorCode(err)})
		return
	}
	s.log(request, string(status.State))
	s.writeResponse(connection, limits, Response{Status: &status})
}

func (s Server) prepareSocketDirectory(dir string) error {
	mode := os.FileMode(0o700)
	if s.SocketGroupSet {
		mode = 0o750
	}
	if err := os.MkdirAll(dir, mode); err != nil {
		return err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return errors.New("runtime socket parent must be a directory")
	}
	if s.SocketGroupSet {
		if err := os.Chown(dir, -1, s.SocketGroupID); err != nil {
			return err
		}
	}
	return os.Chmod(dir, mode)
}

func (s Server) authorizePeer(peer peerIdentity) bool {
	if peer.UID == os.Geteuid() {
		return true
	}
	return s.SocketGroupSet && peer.GID == s.SocketGroupID
}

func (s Server) authorizeDestructivePeer(peer peerIdentity) bool {
	allowedUID := os.Geteuid()
	if s.DestructiveSet {
		allowedUID = s.DestructiveUID
	}
	return peer.UID == allowedUID
}

func acquireSocketLock(path string) (*os.File, error) {
	lock, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		return nil, errors.New("runtime manager socket is already owned")
	}
	return lock, nil
}

func removeStaleSocket(path string) error {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSocket == 0 {
		return errors.New("runtime socket path exists and is not a socket")
	}
	connection, err := net.DialTimeout("unix", path, 100*time.Millisecond)
	if err == nil {
		connection.Close()
		return errors.New("runtime manager socket is already accepting connections")
	}
	if !errors.Is(err, syscall.ECONNREFUSED) && !errors.Is(err, syscall.ENOENT) {
		return fmt.Errorf("refusing to replace unprobeable runtime socket: %w", err)
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func requestErrorCode(err error) string {
	if errors.Is(err, os.ErrDeadlineExceeded) || errors.Is(err, context.DeadlineExceeded) {
		return "request_timeout"
	}
	return "invalid_request"
}

func (s Server) writeResponse(connection net.Conn, limits limits, response Response) {
	_ = connection.SetWriteDeadline(time.Now().Add(limits.writeTimeout))
	_ = json.NewEncoder(connection).Encode(response)
}

func (s Server) dispatch(ctx context.Context, request Request) (runtime.Status, error) {
	lifecycle := runtime.Request{WorkspaceID: request.WorkspaceID, Generation: request.Generation}
	switch request.Operation {
	case "ensure_running":
		return s.Driver.EnsureRunning(ctx, lifecycle)
	case "health":
		return s.Driver.Health(ctx, lifecycle)
	case "drain":
		return s.Driver.Drain(ctx, lifecycle)
	case "stop":
		return s.Driver.Stop(ctx, lifecycle)
	case "delete":
		if err := s.Driver.Delete(ctx, lifecycle); err != nil {
			return runtime.Status{}, err
		}
		return runtime.Status{WorkspaceID: lifecycle.WorkspaceID, Generation: lifecycle.Generation, State: runtime.StateAbsent}, nil
	case "delete_tenant_data":
		if err := s.Driver.DeleteTenantData(ctx, lifecycle); err != nil {
			return runtime.Status{}, err
		}
		return runtime.Status{WorkspaceID: lifecycle.WorkspaceID, Generation: lifecycle.Generation, State: runtime.StateAbsent}, nil
	default:
		return runtime.Status{}, errors.New("unsupported lifecycle operation")
	}
}

func (s Server) log(request Request, result string) {
	if s.Logger == nil {
		return
	}
	operation := request.Operation
	if operation != "ensure_running" && operation != "health" && operation != "drain" && operation != "stop" && operation != "delete" && operation != "delete_tenant_data" {
		operation = "invalid"
	}
	workspaceID := request.WorkspaceID
	if !regexp.MustCompile(`^[a-z0-9][a-z0-9-]{2,62}$`).MatchString(workspaceID) {
		workspaceID = "invalid"
	}
	// Deliberately content-free: validated IDs, operation, generation and result only.
	s.Logger.Printf("runtime_lifecycle operation=%s workspace=%s generation=%d result=%s", operation, workspaceID, request.Generation, result)
}

func errorCode(err error) string {
	switch {
	case errors.Is(err, runtime.ErrUnknownWorkspace):
		return "unknown_workspace"
	case errors.Is(err, runtime.ErrInvalidWorkspace):
		return "invalid_workspace"
	case errors.Is(err, runtime.ErrInvalidGeneration):
		return "invalid_generation"
	case errors.Is(err, runtime.ErrStaleGeneration):
		return "stale_generation"
	case errors.Is(err, runtime.ErrGenerationConflict):
		return "generation_conflict"
	case errors.Is(err, runtime.ErrNotFound):
		return "not_found"
	case errors.Is(err, runtime.ErrTenantDataDeleted):
		return "tenant_data_deleted"
	default:
		return "lifecycle_failed"
	}
}

func (r Request) Valid() bool {
	return strings.TrimSpace(r.Operation) != "" && strings.TrimSpace(r.WorkspaceID) != "" && r.Generation > 0
}
