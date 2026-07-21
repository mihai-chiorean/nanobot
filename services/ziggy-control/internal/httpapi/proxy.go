package httpapi

import (
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/telemetry"
)

func NewReverseProxy(target *url.URL, logger *slog.Logger, observability *telemetry.Recorder) *httputil.ReverseProxy {
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
			request.Out.Header.Del("X-Forwarded-For")
			request.SetXForwarded()
			request.Out.Host = target.Host
			request.Out.Header.Del("X-Nanobot-Auth")
			request.Out.Header.Del("X-Ziggy-Subject")
			request.Out.Header.Del("X-Ziggy-Email")
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
