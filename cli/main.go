// Command stackd is the command-line client for the Stackd API (SPECS §24).
package main

import (
	"context"
	"os"
	"os/signal"
	"syscall"

	"github.com/fmiquel90/stackd/cli/internal/cmd"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	// After the first signal, restore the default handling: a command blocked outside the context
	// (reading stdin) is then killed by a second Ctrl-C instead of ignoring it.
	go func() {
		<-ctx.Done()
		stop()
	}()
	code := cmd.Execute(ctx)
	stop()
	os.Exit(code)
}
