package config

import "testing"

func TestLoadProductionDefaultsUseLoopbackHTTPS(t *testing.T) {
	env, files := validEnv("production")
	setProductionRequirements(env, files)
	cfg, err := LoadFrom(mapLookup(env), mapRead(files))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.OAuthIssuerURL != "https://127.0.0.1:8790" || cfg.MCPResourceURL != "https://127.0.0.1:8790/mcp" {
		t.Fatalf("production connector URLs = %q, %q", cfg.OAuthIssuerURL, cfg.MCPResourceURL)
	}
}

func TestLoadRejectsProductionHTTPConnectorURLs(t *testing.T) {
	for name, value := range map[string]string{
		"ZIGGY_CONNECTORS_OAUTH_ISSUER_URL": "http://127.0.0.1:8790",
		"ZIGGY_CONNECTORS_MCP_RESOURCE_URL": "http://127.0.0.1:8790/mcp",
	} {
		t.Run(name, func(t *testing.T) {
			env, files := validEnv("production")
			setProductionRequirements(env, files)
			env[name] = value
			if _, err := LoadFrom(mapLookup(env), mapRead(files)); err == nil {
				t.Fatalf("accepted insecure production %s", name)
			}
		})
	}
}

func TestLoadRejectsMissingProductionTLSFiles(t *testing.T) {
	for _, name := range []string{"ZIGGY_CONNECTORS_TLS_CERT_FILE", "ZIGGY_CONNECTORS_TLS_KEY_FILE"} {
		t.Run(name, func(t *testing.T) {
			env, files := validEnv("production")
			setProductionRequirements(env, files)
			delete(env, name)
			if _, err := LoadFrom(mapLookup(env), mapRead(files)); err == nil {
				t.Fatalf("accepted production configuration without %s", name)
			}
		})
	}
}

func TestLoadAllowsDevelopmentHTTPConnectorURLs(t *testing.T) {
	env, files := validEnv("development")
	cfg, err := LoadFrom(mapLookup(env), mapRead(files))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.OAuthIssuerURL != "http://127.0.0.1:8790" || cfg.MCPResourceURL != "http://127.0.0.1:8790/mcp" {
		t.Fatalf("development connector URLs = %q, %q", cfg.OAuthIssuerURL, cfg.MCPResourceURL)
	}
}

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
	setProductionRequirements(env, files)
	if _, err := LoadFrom(mapLookup(env), mapRead(files)); err == nil {
		t.Fatal("accepted public production listener")
	}
}

func setProductionRequirements(env map[string]string, files map[string][]byte) {
	env["ZIGGY_CONNECTORS_DATABASE_URL_FILE"] = "/database"
	env["ZIGGY_CONNECTORS_TLS_CERT_FILE"] = "/tls-cert"
	env["ZIGGY_CONNECTORS_TLS_KEY_FILE"] = "/tls-key"
	files["/database"] = []byte("postgres://example")
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
