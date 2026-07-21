package httpapi

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

type ReadinessChecker interface {
	Check(context.Context) error
}

type HTTPReadinessChecker struct {
	client  *http.Client
	target  *url.URL
	timeout time.Duration
}

func NewHTTPReadinessChecker(target *url.URL, timeout time.Duration) *HTTPReadinessChecker {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.ResponseHeaderTimeout = timeout
	return &HTTPReadinessChecker{
		client:  &http.Client{Transport: transport},
		target:  target,
		timeout: timeout,
	}
}

func (checker *HTTPReadinessChecker) Check(ctx context.Context) error {
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

	if response.StatusCode >= http.StatusInternalServerError {
		return fmt.Errorf("upstream returned %s", response.Status)
	}
	return nil
}
