//go:build !linux

package api

import (
	"net"
	"os"
)

// Filesystem permissions remain the non-Linux development boundary. Linux
// production builds replace this with SO_PEERCRED enforcement.
func socketPeerIdentity(net.Conn) (peerIdentity, error) {
	return peerIdentity{UID: os.Geteuid(), GID: os.Getegid(), PID: os.Getpid()}, nil
}
