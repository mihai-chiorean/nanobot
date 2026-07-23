package config

import "testing"

func TestTenantStreamLimitCannotExceedGlobalLimit(t *testing.T) {
	values := map[string]string{
		"ZIGGY_WORK_ENV":                 "test",
		"ZIGGY_WORK_STREAM_LIMIT":        "3",
		"ZIGGY_WORK_TENANT_STREAM_LIMIT": "4",
	}
	lookup := func(key string) (string, bool) {
		value, ok := values[key]
		return value, ok
	}
	if _, err := LoadFrom(lookup, nil); err == nil {
		t.Fatal("tenant stream limit greater than global limit was accepted")
	}
	values["ZIGGY_WORK_TENANT_STREAM_LIMIT"] = "2"
	config, err := LoadFrom(lookup, nil)
	if err != nil {
		t.Fatal(err)
	}
	if config.StreamLimit != 3 || config.TenantStreamLimit != 2 {
		t.Fatalf("stream limits=%d,%d", config.StreamLimit, config.TenantStreamLimit)
	}
}
