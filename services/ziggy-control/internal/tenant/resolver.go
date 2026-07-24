package tenant

import (
	"context"

	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

// Resolver is the small consumer boundary between identity admission and
// routing. Implementations must derive the allocation only from a verified
// principal or a remembered server-side capability.
type Resolver interface {
	Resolve(context.Context, identity.Principal) (Allocation, error)
	ResolveActive(ctx context.Context, userID, workspaceID string) (Allocation, error)
}
