package provider

import (
	"crypto/sha256"
	"encoding/base64"
)

func base64URLSHA256(value string) string {
	sum := sha256.Sum256([]byte(value))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}
