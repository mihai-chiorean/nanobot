package artifactstore

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/mihai-chiorean/nanobot/services/ziggy-work/internal/model"
)

// Put atomically stores immutable artifact bytes in a tenant-scoped path.
// The returned cleanup function removes only a file created by this call.
func Put(root string, tenant model.Tenant, taskID, artifactID string, source io.Reader, maxBytes, expectedSize int64, expectedSHA256 string) (string, int64, string, func(), error) {
	if !tenant.Valid() || !model.WorkIDPattern.MatchString(taskID) || !safeSegment(artifactID) {
		return "", 0, "", noop, model.ErrInvalidInput
	}
	if maxBytes <= 0 {
		return "", 0, "", noop, errors.New("artifact byte limit must be positive")
	}

	relative := filepath.Join("tenants", tenantKey(tenant), taskID, artifactID)
	destination, err := filepath.Abs(root)
	if err != nil {
		return "", 0, "", noop, err
	}
	target := filepath.Join(destination, relative)
	if err := os.MkdirAll(filepath.Dir(target), 0700); err != nil {
		return "", 0, "", noop, err
	}

	temporary, err := os.CreateTemp(filepath.Dir(target), ".artifact-*")
	if err != nil {
		return "", 0, "", noop, err
	}
	temporaryName := temporary.Name()
	removeTemporary := func() {
		_ = temporary.Close()
		_ = os.Remove(temporaryName)
	}
	if err := temporary.Chmod(0600); err != nil {
		removeTemporary()
		return "", 0, "", noop, err
	}

	hash := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(temporary, hash), io.LimitReader(source, maxBytes+1))
	if copyErr != nil {
		removeTemporary()
		return "", 0, "", noop, fmt.Errorf("copy artifact: %w", copyErr)
	}
	if written > maxBytes {
		removeTemporary()
		return "", 0, "", noop, errors.New("artifact exceeds byte limit")
	}
	digest := hex.EncodeToString(hash.Sum(nil))
	if expectedSize > 0 && written != expectedSize {
		removeTemporary()
		return "", 0, "", noop, errors.New("artifact size mismatch")
	}
	if expectedSHA256 != "" && !strings.EqualFold(expectedSHA256, digest) {
		removeTemporary()
		return "", 0, "", noop, errors.New("artifact sha256 mismatch")
	}
	if err := temporary.Sync(); err != nil {
		removeTemporary()
		return "", 0, "", noop, err
	}
	if err := temporary.Close(); err != nil {
		_ = os.Remove(temporaryName)
		return "", 0, "", noop, err
	}

	if err := os.Link(temporaryName, target); err != nil {
		if !errors.Is(err, os.ErrExist) {
			_ = os.Remove(temporaryName)
			return "", 0, "", noop, err
		}
		existingSize, existingDigest, digestErr := digestFile(target, maxBytes)
		_ = os.Remove(temporaryName)
		if digestErr != nil {
			return "", 0, "", noop, digestErr
		}
		if existingSize != written || !strings.EqualFold(existingDigest, digest) {
			return "", 0, "", noop, errors.New("artifact identifier already contains different bytes")
		}
		return relative, written, digest, noop, nil
	}
	_ = os.Remove(temporaryName)
	if directory, err := os.Open(filepath.Dir(target)); err == nil {
		_ = directory.Sync()
		_ = directory.Close()
	}
	// Published immutable bytes are retained if the metadata transaction fails;
	// a retry reuses them safely, while deleting here could race a successful peer.
	return relative, written, digest, noop, nil
}

func tenantKey(tenant model.Tenant) string {
	digest := sha256.Sum256([]byte(tenant.UserID + "\x00" + tenant.WorkspaceID))
	return hex.EncodeToString(digest[:])
}

func safeSegment(value string) bool {
	return value != "" && value != "." && value != ".." && len(value) <= 128 && !strings.ContainsAny(value, "/\\\x00")
}

func digestFile(path string, maxBytes int64) (int64, string, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, "", err
	}
	defer file.Close()
	hash := sha256.New()
	written, err := io.Copy(hash, io.LimitReader(file, maxBytes+1))
	if err != nil {
		return 0, "", err
	}
	if written > maxBytes {
		return 0, "", errors.New("existing artifact exceeds byte limit")
	}
	return written, hex.EncodeToString(hash.Sum(nil)), nil
}

func noop() {}
