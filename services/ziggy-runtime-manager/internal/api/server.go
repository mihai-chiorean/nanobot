// Package api exposes the manager's deliberately small Unix-socket protocol.
package api

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"log"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"

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
	Driver runtime.Driver
	Logger *log.Logger
}

func (s Server) ListenAndServe(ctx context.Context, socket string) error {
	if s.Driver == nil {
		return errors.New("runtime driver is required")
	}
	if err := os.MkdirAll(filepath.Dir(socket), 0o700); err != nil {
		return err
	}
	if err := os.Remove(socket); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	if err := os.Chmod(socket, 0o660); err != nil {
		listener.Close()
		return err
	}
	defer os.Remove(socket)
	defer listener.Close()
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()
	for {
		connection, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return err
		}
		go s.serveConnection(ctx, connection)
	}
}

func (s Server) serveConnection(ctx context.Context, connection net.Conn) {
	defer connection.Close()
	decoder := json.NewDecoder(bufio.NewReader(connection))
	encoder := json.NewEncoder(connection)
	var request Request
	if err := decoder.Decode(&request); err != nil {
		_ = encoder.Encode(Response{Error: "invalid_request"})
		return
	}
	status, err := s.dispatch(ctx, request)
	if err != nil {
		s.log(request, "failed")
		_ = encoder.Encode(Response{Error: errorCode(err)})
		return
	}
	s.log(request, string(status.State))
	_ = encoder.Encode(Response{Status: &status})
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
	default:
		return runtime.Status{}, errors.New("unsupported lifecycle operation")
	}
}

func (s Server) log(request Request, result string) {
	if s.Logger == nil {
		return
	}
	operation := request.Operation
	if operation != "ensure_running" && operation != "health" && operation != "drain" && operation != "stop" && operation != "delete" {
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
	case errors.Is(err, runtime.ErrInvalidGeneration):
		return "invalid_generation"
	case errors.Is(err, runtime.ErrStaleGeneration):
		return "stale_generation"
	case errors.Is(err, runtime.ErrGenerationConflict):
		return "generation_conflict"
	case errors.Is(err, runtime.ErrNotFound):
		return "not_found"
	default:
		return "lifecycle_failed"
	}
}

func (r Request) Valid() bool {
	return strings.TrimSpace(r.Operation) != "" && strings.TrimSpace(r.WorkspaceID) != "" && r.Generation > 0
}
