package podman

import (
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"
)

const (
	defaultEgressProxy  = "http://egress-proxy:18080"
	minRuntimePort      = 21000
	maxRuntimePort      = 29999
	canaryCapabilityTTL = 15 * time.Minute
	maxCanaryTenants    = 4
)

var (
	workspaceIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{2,62}$`)
	imageDigestPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._/-]*(?::[a-z0-9][a-z0-9._-]*)?@sha256:[a-f0-9]{64}$`)
	capabilityPattern  = regexp.MustCompile(`^[A-Za-z0-9_-]{43,128}$`)
)

// Policy is manager-owned configuration. It must be mode 0600 and is never
// supplied by ziggy-control or copied into a tenant's workspace.
type Policy struct {
	ImageDigest string                  `json:"image_digest"`
	Tenants     map[string]TenantPolicy `json:"tenants"`
}

type TenantPolicy struct {
	HostPort        int        `json:"host_port"`
	Model           string     `json:"model"`
	ModelCapability Capability `json:"model_capability"`
	MCPCapability   Capability `json:"mcp_capability"`
}

// Capability is generation-scoped, short-lived material intended only for the
// model gateway or tenant MCP broker. It must never be a provider, Git, or DB secret.
type Capability struct {
	Value     string    `json:"value"`
	ExpiresAt time.Time `json:"expires_at"`
}

func NewCapability(expiresAt time.Time) (Capability, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return Capability{}, err
	}
	return Capability{Value: base64.RawURLEncoding.EncodeToString(buf), ExpiresAt: expiresAt.UTC()}, nil
}

// Validate applies the Phase 1 canary policy. Production must replace this
// static, short-lived policy file with atomic credential rotation and reload.
func (p Policy) Validate(now time.Time) error {
	return p.validate(now, true)
}

func (p Policy) validate(now time.Time, requireFreshCapabilities bool) error {
	if !imageDigestPattern.MatchString(p.ImageDigest) {
		return errors.New("runtime image must be pinned by sha256 digest")
	}
	if len(p.Tenants) == 0 {
		return errors.New("policy must allocate at least one workspace")
	}
	if len(p.Tenants) > maxCanaryTenants {
		return fmt.Errorf("Phase 1 policy exceeds the %d workspace canary limit", maxCanaryTenants)
	}
	ports := map[int]string{}
	caps := map[string]string{}
	for workspaceID, tenant := range p.Tenants {
		if !workspaceIDPattern.MatchString(workspaceID) {
			return fmt.Errorf("invalid workspace ID %q", workspaceID)
		}
		if tenant.HostPort < minRuntimePort || tenant.HostPort > maxRuntimePort {
			return fmt.Errorf("workspace %s has unsafe host port", workspaceID)
		}
		if other, exists := ports[tenant.HostPort]; exists {
			return fmt.Errorf("workspaces %s and %s share host port %d", other, workspaceID, tenant.HostPort)
		}
		ports[tenant.HostPort] = workspaceID
		if strings.TrimSpace(tenant.Model) == "" || len(tenant.Model) > 256 {
			return fmt.Errorf("workspace %s has invalid model", workspaceID)
		}
		for name, capability := range map[string]Capability{"model": tenant.ModelCapability, "mcp": tenant.MCPCapability} {
			if !capabilityPattern.MatchString(capability.Value) {
				return fmt.Errorf("workspace %s has invalid %s capability", workspaceID, name)
			}
			if requireFreshCapabilities && (!capability.ExpiresAt.After(now.UTC()) || capability.ExpiresAt.After(now.UTC().Add(canaryCapabilityTTL))) {
				return fmt.Errorf("workspace %s has %s capability outside the canary 15 minute window", workspaceID, name)
			}
			if other, exists := caps[capability.Value]; exists {
				return fmt.Errorf("workspace %s reuses capability from %s", workspaceID, other)
			}
			caps[capability.Value] = workspaceID + "/" + name
		}
	}
	return nil
}

func (p Policy) WorkspaceIDs() []string {
	ids := make([]string, 0, len(p.Tenants))
	for id := range p.Tenants {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
