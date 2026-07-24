package atomicfile

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// Write replaces path only after the complete new file is durable. The caller
// must create and secure the parent directory before calling Write.
func Write(path string, data []byte, mode os.FileMode) (resultErr error) {
	dir := filepath.Dir(path)
	temp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	tempPath := temp.Name()
	tempOpen := true
	defer func() {
		if tempOpen {
			if closeErr := temp.Close(); resultErr == nil && closeErr != nil {
				resultErr = closeErr
			}
		}
		if removeErr := os.Remove(tempPath); resultErr == nil && removeErr != nil && !os.IsNotExist(removeErr) {
			resultErr = removeErr
		}
	}()

	if err := temp.Chmod(mode); err != nil {
		return err
	}
	if written, err := temp.Write(data); err != nil {
		return err
	} else if written != len(data) {
		return io.ErrShortWrite
	}
	if err := temp.Sync(); err != nil {
		return err
	}
	if err := temp.Close(); err != nil {
		return err
	}
	tempOpen = false
	if err := os.Rename(tempPath, path); err != nil {
		return err
	}

	directory, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return fmt.Errorf("sync atomic file directory: %w", err)
	}
	return nil
}
