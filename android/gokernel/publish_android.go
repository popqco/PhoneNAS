//go:build android && cgo

package gokernel

/*
#define _GNU_SOURCE
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>

static int publish_new(int dir, const char *stage, const char *name) {
	if (renameat2(dir, stage, dir, name, RENAME_NOREPLACE) == 0) return 0;
	return errno;
}
*/
import "C"

import (
	"os"
	"syscall"
	"unsafe"
)

func publishNew(parent *os.Root, stage, name string) error {
	dir, err := parent.Open(".")
	if err != nil {
		return err
	}
	defer dir.Close()
	src, dst := C.CString(stage), C.CString(name)
	defer C.free(unsafe.Pointer(src))
	defer C.free(unsafe.Pointer(dst))
	// The names are single components in the same pinned parent. If this
	// filesystem cannot honor NOREPLACE, fail instead of using plain rename.
	if errno := C.publish_new(C.int(dir.Fd()), src, dst); errno != 0 {
		return syscall.Errno(errno)
	}
	return nil
}
