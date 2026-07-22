package crypto

import (
	"testing"
	"time"
)

func TestAESGCMRoundTripAndTamper(t *testing.T) {
	cipher, err := NewAESGCM([]byte("01234567890123456789012345678901"))
	if err != nil {
		t.Fatal(err)
	}
	aad := []byte("tenant-a")
	ciphertext, err := cipher.Encrypt([]byte("refresh-token"), aad)
	if err != nil {
		t.Fatal(err)
	}
	plaintext, err := cipher.Decrypt(ciphertext, aad)
	if err != nil || string(plaintext) != "refresh-token" {
		t.Fatalf("decrypt = %q, %v", plaintext, err)
	}
	ciphertext[len(ciphertext)-1] ^= 1
	if _, err := cipher.Decrypt(ciphertext, aad); err == nil {
		t.Fatal("tampered ciphertext decrypted")
	}
	if _, err := cipher.Decrypt(ciphertext, []byte("tenant-b")); err == nil {
		t.Fatal("ciphertext decrypted under another tenant context")
	}
}

func TestStateSignerRejectsTamperingAndExpiry(t *testing.T) {
	signer := NewStateSigner([]byte("01234567890123456789012345678901"))
	now := time.Unix(1000, 0)
	state, err := signer.Sign(StateClaims{TransactionID: "tx", UserID: "u", WorkspaceID: "w", Nonce: "n", ExpiresAt: now.Add(time.Minute).Unix()})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := signer.Verify(state+"x", now); err == nil {
		t.Fatal("tampered state accepted")
	}
	if _, err := signer.Verify(state, now.Add(2*time.Minute)); err == nil {
		t.Fatal("expired state accepted")
	}
}
