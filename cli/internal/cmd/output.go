package cmd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
	"text/tabwriter"
)

// printJSON writes an API body indented (it is already JSON; re-indenting keeps it stable).
func printJSON(w io.Writer, raw []byte) error {
	if len(raw) == 0 {
		return nil
	}
	var buf bytes.Buffer
	if err := json.Indent(&buf, raw, "", "  "); err != nil {
		_, err = w.Write(raw)
		return err
	}
	buf.WriteByte('\n')
	_, err := w.Write(buf.Bytes())
	return err
}

// table prints rows aligned in columns under an upper-case header.
func table(w io.Writer, header []string, rows [][]string) error {
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	fmt.Fprintln(tw, strings.Join(header, "\t"))
	for _, r := range rows {
		fmt.Fprintln(tw, strings.Join(r, "\t"))
	}
	return tw.Flush()
}

// fields prints "key: value" pairs, aligned, in the given order.
func fields(w io.Writer, kv [][2]string) error {
	tw := tabwriter.NewWriter(w, 0, 0, 1, ' ', 0)
	for _, p := range kv {
		fmt.Fprintf(tw, "%s:\t%s\n", p[0], p[1])
	}
	return tw.Flush()
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func yesNo(b bool) string {
	if b {
		return "yes"
	}
	return "no"
}

func short(sha *string) string {
	s := deref(sha)
	if len(s) > 12 {
		return s[:12]
	}
	return s
}

// labelsString renders a labels map as sorted k=v pairs.
func labelsString(m map[string]any) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%v", k, m[k]))
	}
	return strings.Join(parts, ",")
}

// parseLabels turns repeated "key=value" flags into a labels object.
func parseLabels(pairs []string) (map[string]any, error) {
	if len(pairs) == 0 {
		return nil, nil
	}
	m := make(map[string]any, len(pairs))
	for _, p := range pairs {
		k, v, ok := strings.Cut(p, "=")
		if !ok || k == "" {
			return nil, fmt.Errorf("invalid label %q: expected key=value", p)
		}
		m[k] = v
	}
	return m, nil
}

// planSummary renders {"add": 1, "change": 0, …} as "+1 ~0 -0" (plus import/forget when set).
func planSummary(s map[string]any) string {
	if s == nil {
		return ""
	}
	n := func(k string) string {
		if v, ok := s[k].(float64); ok {
			return fmt.Sprintf("%d", int(v))
		}
		return "0"
	}
	out := fmt.Sprintf("+%s ~%s -%s", n("add"), n("change"), n("destroy"))
	for _, k := range []string{"import", "forget"} {
		if v, ok := s[k].(float64); ok && v > 0 {
			out += fmt.Sprintf(" %s:%d", k, int(v))
		}
	}
	return out
}
