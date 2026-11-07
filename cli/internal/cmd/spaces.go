package cmd

import (
	"fmt"
	"net/http"

	"github.com/spf13/cobra"

	"github.com/fmiquel90/stackd/cli/internal/client"
)

func newSpacesCmd(a *app) *cobra.Command {
	cmd := &cobra.Command{Use: "spaces", Aliases: []string{"space"}, Short: "List and create spaces"}

	list := &cobra.Command{
		Use:   "list",
		Short: "List the spaces this token can reach",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			var spaces []client.Space
			raw, err := c.JSON(cmd.Context(), http.MethodGet, "/spaces", nil, nil, &spaces)
			if err != nil {
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			rows := make([][]string, 0, len(spaces))
			for _, s := range spaces {
				rows = append(rows, []string{s.ID, s.Name, deref(s.Description)})
			}
			return table(a.out, []string{"ID", "NAME", "DESCRIPTION"}, rows)
		},
	}

	var description string
	create := &cobra.Command{
		Use:   "create <name>",
		Short: "Create a space (instance admin only)",
		Long: `Create a space. The API reserves this to an instance admin: a service-account token,
capped at writer, gets 403 (SPECS §23). The creator becomes the space's admin.`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			c, err := a.client()
			if err != nil {
				return err
			}
			body := map[string]any{"name": args[0]}
			if description != "" {
				body["description"] = description
			}
			var s client.Space
			raw, err := c.JSON(cmd.Context(), http.MethodPost, "/spaces", nil, body, &s)
			if err != nil {
				if client.IsStatus(err, http.StatusForbidden) {
					return fmt.Errorf("%w\n(creating a space requires an instance admin; a service account cannot)", err)
				}
				return err
			}
			if a.json() {
				return printJSON(a.out, raw)
			}
			fmt.Fprintf(a.out, "Created space %s (%s)\n", s.Name, s.ID)
			return nil
		},
	}
	create.Flags().StringVar(&description, "description", "", "space description")

	cmd.AddCommand(list, create)
	return cmd
}
