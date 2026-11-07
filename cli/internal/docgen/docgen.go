// Package docgen renders the CLI's command reference (docs/guide/cli-reference.md) from the
// cobra command tree, so the published reference can never drift from `stackd --help`.
package docgen

import (
	"bytes"
	"strings"

	"github.com/spf13/cobra"
	"github.com/spf13/cobra/doc"
)

const header = `# CLI reference

<!-- Generated from the command tree by cli/internal/docgen — do not edit by hand.
     Regenerate with: task cli-docs -->

Every ` + "`stackd`" + ` command and flag, as ` + "`stackd <command> --help`" + ` prints them. For a guided tour
(install, authentication, common tasks, CI), read the [command-line client guide](cli.md).

`

// Reference renders one Markdown page holding every available command, depth first. Links
// between commands become in-page anchors (MkDocs slugs "## stackd runs plan" as
// "stackd-runs-plan").
func Reference(root *cobra.Command) ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteString(header)
	link := func(file string) string {
		return "#" + strings.ReplaceAll(strings.TrimSuffix(file, ".md"), "_", "-")
	}
	var walk func(c *cobra.Command) error
	walk = func(c *cobra.Command) error {
		if !c.IsAvailableCommand() && c != root || c.IsAdditionalHelpTopicCommand() {
			return nil
		}
		c.DisableAutoGenTag = true // no generation date: the output must be reproducible
		if err := doc.GenMarkdownCustom(c, &buf, link); err != nil {
			return err
		}
		for _, sub := range c.Commands() {
			if err := walk(sub); err != nil {
				return err
			}
		}
		return nil
	}
	if err := walk(root); err != nil {
		return nil, err
	}
	// Keep the page's table of contents to one entry per command: cobra's per-command
	// sub-headings (Synopsis, Options, Examples, SEE ALSO…) become bold lines.
	lines := strings.Split(strings.TrimRight(buf.String(), "\n"), "\n")
	inCode := false
	for i, ln := range lines {
		if strings.HasPrefix(ln, "```") {
			inCode = !inCode
		}
		if !inCode && strings.HasPrefix(ln, "### ") {
			lines[i] = "**" + strings.TrimPrefix(ln, "### ") + "**"
		}
	}
	return []byte(strings.Join(lines, "\n")), nil
}
