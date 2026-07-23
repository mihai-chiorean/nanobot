package config

import "testing"

func TestLoadRejectsProductionHTTPRedirect(t *testing.T) {
	env, files := validEnv("production")
	env["ZIGGY_CONNECTORS_GOOGLE_REDIRECT_URI"] = "http://localhost/callback"
	if _, err := LoadFrom(mapLookup(env), mapRead(files)); err == nil {
		t.Fatal("accepted insecure production redirect URI")
	}
}

func TestLoadRejectsMissingTrustKey(t *testing.T) {
	env, files := validEnv("test")
	delete(files, "/trust")
	if _, err := LoadFrom(mapLookup(env), mapRead(files)); err == nil {
		t.Fatal("accepted missing trust key")
	}
}

func TestLoadRejectsInvalidListenAddress(t *testing.T) {
	env, files := validEnv("test")
	env["ZIGGY_CONNECTORS_LISTEN_ADDR"] = "not-an-address"
	if _, err := LoadFrom(mapLookup(env), mapRead(files)); err == nil {
		t.Fatal("accepted invalid listen address")
	}
}

func TestLoadRejectsPublicProductionListener(t *testing.T) {
	env, files := validEnv("production")
	env["ZIGGY_CONNECTORS_LISTEN_ADDR"] = "0.0.0.0:8790"
	env["ZIGGY_CONNECTORS_DATABASE_URL_FILE"] = "/database"
	files["/database"] = []byte("postgres://example")
	if _, err := LoadFrom(mapLookup(env), mapRead(files)); err == nil {
		t.Fatal("accepted public production listener")
	}
}

func validEnv(environment string) (map[string]string, map[string][]byte) {
	return map[string]string{
			"ZIGGY_CONNECTORS_ENV":                           environment,
			"ZIGGY_CONNECTORS_LISTEN_ADDR":                   "127.0.0.1:8790",
			"ZIGGY_CONNECTORS_GOOGLE_CLIENT_ID":              "client",
			"ZIGGY_CONNECTORS_GOOGLE_REDIRECT_URI":           "https://example.test/callback",
			"ZIGGY_CONNECTORS_GOOGLE_CLIENT_SECRET_FILE":     "/client",
			"ZIGGY_CONNECTORS_STATE_SIGNING_KEY_FILE":        "/state",
			"ZIGGY_CONNECTORS_TOKEN_ENCRYPTION_KEY_FILE":     "/token",
			"ZIGGY_CONNECTORS_TRUST_KEY_FILE":                "/trust",
			"ZIGGY_CONNECTORS_CLIENT_CREDENTIAL_PEPPER_FILE": "/pepper",
			"ZIGGY_CONNECTORS_MCP_ACCESS_SIGNING_KEY_FILE":   "/mcp-signing",
		}, map[string][]byte{
			"/client":      []byte("secret"),
			"/state":       []byte("01234567890123456789012345678901"),
			"/token":       []byte("01234567890123456789012345678901"),
			"/trust":       []byte("01234567890123456789012345678901"),
			"/pepper":      []byte("01234567890123456789012345678901"),
			"/mcp-signing": []byte("01234567890123456789012345678901"),
		}
}

func mapLookup(values map[string]string) LookupEnv {
	return func(key string) (string, bool) { value, ok := values[key]; return value, ok }
}

func mapRead(values map[string][]byte) ReadFile {
	return func(name string) ([]byte, error) {
		value, ok := values[name]
		if !ok {
			return nil, errMissingFile{}
		}
		return value, nil
	}
}

type errMissingFile struct{}

func (errMissingFile) Error() string { return "missing" }
