//go:build linux

package api

import (
	"errors"
	"net"
	"syscall"
)

func socketPeerIdentity(connection net.Conn) (peerIdentity, error) {
	syscallConnection, ok := connection.(syscall.Conn)
	if !ok {
		return peerIdentity{}, errors.New("Unix connection does not expose peer credentials")
	}
	raw, err := syscallConnection.SyscallConn()
	if err != nil {
		return peerIdentity{}, err
	}
	var credentials *syscall.Ucred
	var credentialErr error
	if err := raw.Control(func(fd uintptr) {
		credentials, credentialErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	}); err != nil {
		return peerIdentity{}, err
	}
	if credentialErr != nil {
		return peerIdentity{}, credentialErr
	}
	return peerIdentity{
		UID: int(credentials.Uid),
		GID: int(credentials.Gid),
		PID: int(credentials.Pid),
	}, nil
}
