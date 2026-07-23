package tenant

import (
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"strings"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
)

var ErrNotFound = errors.New("tenant mapping not found")

type Allocation struct {
	UserID                  string `json:"user_id"`
	WorkspaceID             string `json:"workspace_id"`
	UpstreamURL             string `json:"upstream_url"`
	UpstreamBootstrapSecret string `json:"upstream_bootstrap_secret"`
	Status                  string `json:"status"`
}

type manifest struct {
	Version int          `json:"version"`
	Tenants []Allocation `json:"tenants"`
}
type Registry struct{ byKey map[string]Allocation }

func (r *Registry) Allocations() []Allocation {
	out := make([]Allocation, 0, len(r.byKey))
	for _, allocation := range r.byKey {
		out = append(out, allocation)
	}
	return out
}

func Load(filename string) (*Registry, error) {
	b, err := os.ReadFile(filename)
	if err != nil {
		return nil, fmt.Errorf("read tenant manifest: %w", err)
	}
	var m manifest
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		return nil, fmt.Errorf("decode tenant manifest: %w", err)
	}
	if m.Version != 1 || len(m.Tenants) == 0 {
		return nil, errors.New("tenant manifest version or tenants are invalid")
	}
	r := &Registry{byKey: make(map[string]Allocation, len(m.Tenants))}
	for _, a := range m.Tenants {
		a.UserID, a.WorkspaceID, a.UpstreamURL, a.UpstreamBootstrapSecret, a.Status = strings.TrimSpace(a.UserID), strings.TrimSpace(a.WorkspaceID), strings.TrimSpace(a.UpstreamURL), strings.TrimSpace(a.UpstreamBootstrapSecret), strings.ToLower(strings.TrimSpace(a.Status))
		if a.UserID == "" || a.WorkspaceID == "" || a.Status != "active" || len(a.UpstreamBootstrapSecret) < 32 {
			return nil, errors.New("invalid active tenant allocation")
		}
		if err := validateURL(a.UpstreamURL); err != nil {
			return nil, fmt.Errorf("tenant %s upstream_url: %w", a.UserID, err)
		}
		key := a.UserID + "\x00" + a.WorkspaceID
		if _, exists := r.byKey[key]; exists {
			return nil, errors.New("duplicate tenant allocation")
		}
		r.byKey[key] = a
	}
	return r, nil
}

func (r *Registry) Resolve(t model.Tenant) (Allocation, error) {
	a, ok := r.byKey[t.UserID+"\x00"+t.WorkspaceID]
	if !ok {
		return Allocation{}, ErrNotFound
	}
	return a, nil
}

func validateURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "http" && u.Scheme != "https" || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return errors.New("must be an absolute private URL without credentials, query, or fragment")
	}
	host := u.Hostname()
	if strings.EqualFold(host, "localhost") {
		return nil
	}
	ip := net.ParseIP(host)
	if ip == nil || !privateIP(ip) {
		return errors.New("host must be loopback, RFC1918, ULA, or Tailscale CGNAT")
	}
	return nil
}

func privateIP(ip net.IP) bool {
	if ip.IsLoopback() {
		return true
	}
	_, n10, _ := net.ParseCIDR("10.0.0.0/8")
	_, n172, _ := net.ParseCIDR("172.16.0.0/12")
	_, n192, _ := net.ParseCIDR("192.168.0.0/16")
	_, ntailscale, _ := net.ParseCIDR("100.64.0.0/10")
	_, nula, _ := net.ParseCIDR("fc00::/7")
	return n10.Contains(ip) || n172.Contains(ip) || n192.Contains(ip) || ntailscale.Contains(ip) || nula.Contains(ip)
}
