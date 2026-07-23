package httpapi

import (
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/telemetry"
)

func NewReverseProxy(target *url.URL, logger *slog.Logger, observability *telemetry.Recorder) *httputil.ReverseProxy {
	return newReverseProxy(target, logger, observability, proxyKindNanobot)
}

func NewConnectorReverseProxy(target *url.URL, logger *slog.Logger, observability *telemetry.Recorder) *httputil.ReverseProxy {
	return newReverseProxy(target, logger, observability, proxyKindConnector)
}

type proxyKind uint8

const (
	proxyKindNanobot proxyKind = iota
	proxyKindConnector
	proxyKindWork
)

func newReverseProxy(target *url.URL, logger *slog.Logger, observability *telemetry.Recorder, kind proxyKind) *httputil.ReverseProxy {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConns = 100
	transport.MaxIdleConnsPerHost = 32
	transport.MaxConnsPerHost = 256
	transport.IdleConnTimeout = 90 * time.Second
	transport.ResponseHeaderTimeout = 30 * time.Second
	transport.TLSHandshakeTimeout = 10 * time.Second
	transport.DialContext = (&net.Dialer{
		Timeout:   5 * time.Second,
		KeepAlive: 30 * time.Second,
	}).DialContext

	if observability == nil {
		observability = telemetry.Noop()
	}
	proxy := &httputil.ReverseProxy{
		Transport:     observability.WrapTransport(transport, ""),
		FlushInterval: -1,
		Rewrite: func(request *httputil.ProxyRequest) {
			request.SetURL(target)
			if kind == proxyKindConnector {
				request.Out.URL.Path = strings.TrimPrefix(request.In.URL.Path, "/connectors")
				if request.Out.URL.Path == "" {
					request.Out.URL.Path = "/"
				}
				request.Out.URL.RawPath = ""
			}
			stripUntrustedUpstreamHeaders(request.Out.Header)
			if kind == proxyKindWork {
				stripUntrustedWorkHeaders(request.Out.Header)
				request.Out.Header.Del("Authorization")
				if principal, ok := workPrincipalFromContext(request.In.Context()); ok {
					request.Out.Header.Set("X-Ziggy-Principal", principal.payload)
					request.Out.Header.Set("X-Ziggy-Principal-Signature", principal.signature)
				}
			}
			if kind == proxyKindConnector {
				if principal, ok := connectorPrincipalFromContext(request.In.Context()); ok {
					request.Out.Header.Set("X-Ziggy-Principal", principal.payload)
					request.Out.Header.Set("X-Ziggy-Principal-Signature", principal.signature)
				}
			}
			request.SetXForwarded()
			request.Out.Host = target.Host
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			logger.ErrorContext(r.Context(), "upstream request failed",
				"route", routeName(r.URL.Path),
				"error_class", requestErrorClass(err),
			)
			writeError(w, http.StatusBadGateway, "upstream unavailable")
		},
	}
	return proxy
}

func stripUntrustedUpstreamHeaders(headers http.Header) {
	for key := range headers {
		lower := strings.ToLower(key)
		if lower == "cookie" || lower == "proxy-authorization" || lower == "forwarded" || strings.HasPrefix(lower, "x-forwarded-") {
			headers.Del(key)
		}
	}
	for _, key := range []string{
		"X-Nanobot-Auth",
		"X-Ziggy-Subject",
		"X-Ziggy-Email",
		"X-Ziggy-Principal",
		"X-Ziggy-Principal-Signature",
		"X-Clerk-Session",
		"X-Clerk-User",
		"X-User-Email",
		"X-User-Subject",
	} {
		headers.Del(key)
	}
}

func stripUntrustedWorkHeaders(headers http.Header) {
	for key := range headers {
		if strings.HasPrefix(strings.ToLower(key), "x-ziggy-") {
			headers.Del(key)
		}
	}
}
