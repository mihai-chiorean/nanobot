package httpapi

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"path"
	"strings"
	"sync/atomic"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/telemetry"
)

type ReadinessChecker interface {
	Check(context.Context) error
}

type HTTPReadinessChecker struct {
	client     *http.Client
	origin     *url.URL
	target     *url.URL
	timeout    time.Duration
	cacheTTL   time.Duration
	refreshing atomic.Bool
	snapshot   atomic.Pointer[readinessSnapshot]
}

type readinessSnapshot struct {
	checkedAt time.Time
	err       error
}

type upstreamStatusError struct {
	status string
}

func (err upstreamStatusError) Error() string {
	return "upstream returned " + err.status
}

var errReadinessRefreshInProgress = fmt.Errorf("readiness refresh in progress")

func NewHTTPReadinessChecker(
	target *url.URL,
	readyPath string,
	timeout, cacheTTL time.Duration,
	observability *telemetry.Recorder,
) *HTTPReadinessChecker {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = timeout
	if observability == nil {
		observability = telemetry.Noop()
	}
	probeTarget := *target
	probeTarget.Path = readyPath
	probeTarget.RawPath = ""
	probeTarget.RawQuery = ""
	probeTarget.Fragment = ""
	return &HTTPReadinessChecker{
		client:   &http.Client{Transport: observability.WrapTransport(transport, "readiness")},
		origin:   cloneURL(target),
		target:   &probeTarget,
		timeout:  timeout,
		cacheTTL: cacheTTL,
	}
}

// Preflight verifies the configured health path and the private runtime's
// authenticated bootstrap contract before the front door starts accepting traffic.
func (checker *HTTPReadinessChecker) Preflight(ctx context.Context) error {
	if err := checker.check(ctx); err != nil {
		return fmt.Errorf("configured readiness path: %w", err)
	}
	return checker.checkContractRoute(ctx, "/auth/bootstrap", http.StatusUnauthorized)
}

func (checker *HTTPReadinessChecker) checkContractRoute(ctx context.Context, route string, wantStatus int) error {
	ctx, cancel := context.WithTimeout(ctx, checker.timeout)
	defer cancel()
	target := *checker.origin
	target.Path = path.Join(strings.TrimSuffix(target.Path, "/"), route)
	target.RawPath = ""
	target.RawQuery = ""
	target.Fragment = ""
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return fmt.Errorf("create upstream contract request %s: %w", route, err)
	}
	response, err := checker.client.Do(request)
	if err != nil {
		return fmt.Errorf("upstream contract %s unavailable: %w", route, err)
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4<<10))
	if response.StatusCode != wantStatus {
		return fmt.Errorf("upstream contract %s returned %s, want %d", route, response.Status, wantStatus)
	}
	return nil
}

func cloneURL(source *url.URL) *url.URL {
	clone := *source
	return &clone
}

func (checker *HTTPReadinessChecker) Check(ctx context.Context) error {
	if snapshot := checker.snapshot.Load(); snapshot != nil && time.Since(snapshot.checkedAt) < checker.cacheTTL {
		return snapshot.err
	}
	if !checker.refreshing.CompareAndSwap(false, true) {
		if snapshot := checker.snapshot.Load(); snapshot != nil {
			return snapshot.err
		}
		return errReadinessRefreshInProgress
	}
	defer checker.refreshing.Store(false)

	err := checker.check(ctx)
	checker.snapshot.Store(&readinessSnapshot{checkedAt: time.Now(), err: err})
	return err
}

func (checker *HTTPReadinessChecker) check(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, checker.timeout)
	defer cancel()

	request, err := http.NewRequestWithContext(ctx, http.MethodGet, checker.target.String(), nil)
	if err != nil {
		return fmt.Errorf("create readiness request: %w", err)
	}
	response, err := checker.client.Do(request)
	if err != nil {
		return fmt.Errorf("reach upstream: %w", err)
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4<<10))

	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return upstreamStatusError{status: response.Status}
	}
	return nil
}

func requestErrorClass(err error) string {
	switch {
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	}
	var statusError upstreamStatusError
	if errors.As(err, &statusError) {
		return "upstream_status"
	}
	var networkError net.Error
	if errors.As(err, &networkError) {
		if networkError.Timeout() {
			return "timeout"
		}
		return "network"
	}
	return "upstream_transport"
}
