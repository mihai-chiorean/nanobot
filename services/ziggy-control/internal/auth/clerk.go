package auth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/clerk/clerk-sdk-go/v2"
	clerkhttp "github.com/clerk/clerk-sdk-go/v2/http"
	"github.com/clerk/clerk-sdk-go/v2/jwks"
	"github.com/clerk/clerk-sdk-go/v2/user"
	"github.com/mihai-chiorean/nanobot/services/ziggy-control/internal/identity"
)

type Config struct {
	SecretKey         string
	AuthorizedParties []string
	HTTPClient        *http.Client
}

type emailResolver func(context.Context, string) (string, error)

type customClaims struct {
	Email        string `json:"email"`
	PrimaryEmail string `json:"primaryEmail"`
}

func New(config Config) (func(http.Handler) http.Handler, error) {
	if strings.TrimSpace(config.SecretKey) == "" {
		return nil, errors.New("Clerk secret key is required")
	}

	httpClient := config.HTTPClient
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 5 * time.Second}
	}
	clientConfig := &clerk.ClientConfig{BackendConfig: clerk.BackendConfig{
		HTTPClient: httpClient,
		Key:        clerk.String(config.SecretKey),
	}}
	userClient := user.NewClient(clientConfig)
	resolver := func(ctx context.Context, subject string) (string, error) {
		account, err := userClient.Get(ctx, subject)
		if err != nil {
			return "", fmt.Errorf("get Clerk user: %w", err)
		}
		return primaryEmail(account)
	}

	options := []clerkhttp.AuthorizationOption{
		clerkhttp.JWKSClient(jwks.NewClient(clientConfig)),
		clerkhttp.CustomClaimsConstructor(func(context.Context) any { return &customClaims{} }),
		clerkhttp.AuthorizationFailureHandler(http.HandlerFunc(writeUnauthorized)),
	}
	if len(config.AuthorizedParties) > 0 {
		allowed := make(map[string]struct{}, len(config.AuthorizedParties))
		for _, party := range config.AuthorizedParties {
			allowed[party] = struct{}{}
		}
		options = append(options, clerkhttp.AuthorizedParty(func(party string) bool {
			_, ok := allowed[party]
			return ok
		}))
	}

	verify := clerkhttp.WithHeaderAuthorization(options...)
	return func(next http.Handler) http.Handler {
		return verify(withPrincipal(next, resolver))
	}, nil
}

func withPrincipal(next http.Handler, resolveEmail emailResolver) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		claims, ok := clerk.SessionClaimsFromContext(r.Context())
		if !ok || claims == nil || claims.Subject == "" {
			writeUnauthorized(w, r)
			return
		}

		email := emailFromClaims(claims)
		if email == "" {
			var err error
			email, err = resolveEmail(r.Context(), claims.Subject)
			if err != nil {
				writeJSONError(w, http.StatusServiceUnavailable, "identity lookup unavailable")
				return
			}
		}

		principal := identity.Principal{
			Subject: claims.Subject,
			Email:   strings.ToLower(strings.TrimSpace(email)),
		}
		next.ServeHTTP(w, r.WithContext(identity.NewContext(r.Context(), principal)))
	})
}

func emailFromClaims(claims *clerk.SessionClaims) string {
	custom, ok := claims.Custom.(*customClaims)
	if !ok || custom == nil {
		return ""
	}
	if custom.Email != "" {
		return custom.Email
	}
	return custom.PrimaryEmail
}

func primaryEmail(account *clerk.User) (string, error) {
	if account == nil || account.PrimaryEmailAddressID == nil {
		return "", errors.New("Clerk user has no primary email")
	}
	for _, address := range account.EmailAddresses {
		if address != nil && address.ID == *account.PrimaryEmailAddressID {
			if address.Verification == nil || address.Verification.Status != "verified" {
				return "", errors.New("Clerk primary email is not verified")
			}
			if strings.TrimSpace(address.EmailAddress) == "" {
				return "", errors.New("Clerk primary email is empty")
			}
			return address.EmailAddress, nil
		}
	}
	return "", errors.New("Clerk primary email was not returned")
}

func writeUnauthorized(w http.ResponseWriter, _ *http.Request) {
	writeJSONError(w, http.StatusUnauthorized, "authentication required")
}

func writeJSONError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message})
}
