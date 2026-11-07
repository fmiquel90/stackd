// Command stackd is the command-line client for the Stackd API (SPECS §24).
package main

import (
	"context"
	"os"
	"os/signal"

	"github.com/fmiquel90/stackd/cli/internal/cmd"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	code := cmd.Execute(ctx)
	stop()
	os.Exit(code)
}
