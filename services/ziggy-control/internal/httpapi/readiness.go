package httpapi

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync/atomic"
	"time"
)

type ReadinessChecker interface {
	Check(context.Context) error
}

type HTTPReadinessChecker struct {
	client     *http.Client
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

var errReadinessRefreshInProgress = fmt.Errorf("readiness refresh in progress")

func NewHTTPReadinessChecker(target *url.URL, readyPath string, timeout, cacheTTL time.Duration) *HTTPReadinessChecker {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = timeout
	probeTarget := *target
	probeTarget.Path = readyPath
	probeTarget.RawPath = ""
	probeTarget.RawQuery = ""
	probeTarget.Fragment = ""
	return &HTTPReadinessChecker{
		client:   &http.Client{Transport: transport},
		target:   &probeTarget,
		timeout:  timeout,
		cacheTTL: cacheTTL,
	}
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
		return fmt.Errorf("upstream returned %s", response.Status)
	}
	return nil
}
