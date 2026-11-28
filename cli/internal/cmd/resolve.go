package cmd

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/fmiquel90/stackd/cli/internal/client"
)

// Commands accept either an id (UUID) or a name. Names are resolved client-side through the list
// endpoints, which return only what the token can see — so resolution never reveals more than
// the API would.

var uuidRe = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

func isUUID(s string) bool { return uuidRe.MatchString(s) }

func resolveSpace(ctx context.Context, c *client.Client, ref string) (*client.Space, error) {
	var spaces []client.Space
	if _, err := c.JSON(ctx, http.MethodGet, "/spaces", nil, nil, &spaces); err != nil {
		return nil, err
	}
	for i := range spaces {
		if spaces[i].ID == ref || spaces[i].Name == ref {
			return &spaces[i], nil
		}
	}
	return nil, fmt.Errorf("space %q not found (or not visible to this token)", ref)
}

// resolveStack finds a stack by id, or by name (optionally within a space, by id or name).
func resolveStack(ctx context.Context, c *client.Client, ref, spaceRef string) (*client.Stack, error) {
	if isUUID(ref) {
		var s client.Stack
		if _, err := c.JSON(ctx, http.MethodGet, "/stacks/"+ref, nil, nil, &s); err != nil {
			return nil, err
		}
		return &s, nil
	}
	spaceID := ""
	if spaceRef != "" {
		sp, err := resolveSpace(ctx, c, spaceRef)
		if err != nil {
			return nil, err
		}
		spaceID = sp.ID
	}
	var stacks []client.Stack
	if _, err := c.JSON(ctx, http.MethodGet, "/stacks", nil, nil, &stacks); err != nil {
		return nil, err
	}
	var found []client.Stack
	for _, s := range stacks {
		if s.Name == ref && (spaceID == "" || s.SpaceID == spaceID) {
			found = append(found, s)
		}
	}
	switch len(found) {
	case 0:
		return nil, fmt.Errorf("stack %q not found (or not visible to this token)", ref)
	case 1:
		return &found[0], nil
	default:
		return nil, fmt.Errorf("stack name %q exists in %d spaces: pass --space or use the stack id", ref, len(found))
	}
}

// resolveEnv finds an environment by id, or by "stack/env" (stack by name or id).
func resolveEnv(ctx context.Context, c *client.Client, ref, spaceRef string) (*client.Environment, error) {
	if isUUID(ref) {
		var e client.Environment
		if _, err := c.JSON(ctx, http.MethodGet, "/environments/"+ref, nil, nil, &e); err != nil {
			return nil, err
		}
		return &e, nil
	}
	stackRef, envName, ok := strings.Cut(ref, "/")
	if !ok || stackRef == "" || envName == "" {
		return nil, fmt.Errorf("invalid environment %q: expected an id or <stack>/<environment>", ref)
	}
	stack, err := resolveStack(ctx, c, stackRef, spaceRef)
	if err != nil {
		return nil, err
	}
	var envs []client.Environment
	if _, err := c.JSON(ctx, http.MethodGet, "/stacks/"+stack.ID+"/environments", nil, nil, &envs); err != nil {
		return nil, err
	}
	for i := range envs {
		if envs[i].Name == envName {
			return &envs[i], nil
		}
	}
	return nil, fmt.Errorf("environment %q not found in stack %q", envName, stack.Name)
}
