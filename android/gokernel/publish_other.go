//go:build !android || !cgo

package gokernel

import "os"

func publishNew(parent *os.Root, stage, name string) error {
	// A same-directory hard link publishes complete bytes only if the name
	// does not exist. The caller removes the staging name after publication.
	return parent.Link(stage, name)
}
