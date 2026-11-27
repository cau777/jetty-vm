package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"strconv"
	"strings"

	"connectrpc.com/connect"

	jettyv1 "github.com/cau777/jetty-vm/gen/go/jetty/v1"
	"github.com/cau777/jetty-vm/gen/go/jetty/v1/jettyv1connect"
)

func cmdRule(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: jetty rule <list|get|put|delete> ...")
	}
	switch args[0] {
	case "list":
		fs := flag.NewFlagSet("rule list", flag.ContinueOnError)
		vmName := fs.String("vm", "", "")
		globalOnly := fs.Bool("global", false, "")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		resp, err := client.ListRules(ctx, connect.NewRequest(&jettyv1.ListRulesRequest{
			VmName:     *vmName,
			GlobalOnly: *globalOnly,
		}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		for _, r := range resp.Msg.GetRules() {
			scope := r.GetScope()
			if scope == "" {
				scope = "global"
			}
			fmt.Printf("%s\t%s\t%d\t%s\t%s\n", scope, r.GetName(), r.GetPriority(), r.GetHostname(), r.GetAction().GetType())
		}
		return nil

	case "get":
		name, rest, err := splitLeadingArg(args[1:])
		if err != nil {
			return fmt.Errorf("usage: jetty rule get NAME [--vm NAME | --global]")
		}
		fs := flag.NewFlagSet("rule get", flag.ContinueOnError)
		vmName := fs.String("vm", "", "")
		globalOnly := fs.Bool("global", false, "")
		if err := fs.Parse(rest); err != nil {
			return err
		}
		req := &jettyv1.GetRuleRequest{Name: name}
		if *globalOnly {
			req.ScopeSpecified = true
			req.Scope = ""
		} else if *vmName != "" {
			req.ScopeSpecified = true
			req.Scope = *vmName
		}
		resp, err := client.GetRule(ctx, connect.NewRequest(req))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		return printProtoJSON(resp.Msg)

	case "put":
		name, rest, err := splitLeadingArg(args[1:])
		if err != nil {
			return fmt.Errorf("usage: jetty rule put NAME (--vm NAME | --global) [--file rule.json | flags]")
		}
		fs := flag.NewFlagSet("rule put", flag.ContinueOnError)
		vmName := fs.String("vm", "", "")
		globalOnly := fs.Bool("global", false, "")
		filePath := fs.String("file", "", "")
		priority := fs.Int("priority", -1, "")
		hostname := fs.String("hostname", "", "")
		actionType := fs.String("action", "", "")
		credential := fs.String("credential", "", "")
		pathPrefix := fs.String("path-prefix", "/", "")
		injType := fs.String("injection", "bearer", "")
		injUser := fs.String("username", "", "")
		injHeader := fs.String("header-name", "", "")
		injService := fs.String("service", "auto", "")
		injRegion := fs.String("region", "auto", "")
		if err := fs.Parse(rest); err != nil {
			return err
		}

		req := &jettyv1.PutRuleRequest{Name: name}
		if *filePath != "" {
			raw, err := os.ReadFile(*filePath)
			if err != nil {
				return err
			}
			var doc struct {
				Priority   int32 `json:"priority"`
				VMSelector *struct {
					Type string   `json:"type"`
					VMs  []string `json:"vms"`
				} `json:"vm_selector"`
				Hostname string `json:"hostname"`
				Action   struct {
					Type       string `json:"type"`
					Credential string `json:"credential"`
					PathPrefix string `json:"path_prefix"`
					Injection  *struct {
						Type     string `json:"type"`
						Username string `json:"username"`
						Name     string `json:"name"`
						Service  string `json:"service"`
						Region   string `json:"region"`
					} `json:"injection"`
				} `json:"action"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				return err
			}
			req.Priority = doc.Priority
			req.Hostname = doc.Hostname
			if doc.VMSelector != nil {
				req.VmSelector = &jettyv1.VmSelector{Type: doc.VMSelector.Type, Vms: doc.VMSelector.VMs}
			}
			act := &jettyv1.RuleAction{
				Type:       doc.Action.Type,
				Credential: doc.Action.Credential,
				PathPrefix: doc.Action.PathPrefix,
			}
			if doc.Action.Injection != nil {
				act.Injection = &jettyv1.Injection{
					Type:     doc.Action.Injection.Type,
					Username: doc.Action.Injection.Username,
					Name:     doc.Action.Injection.Name,
					Service:  doc.Action.Injection.Service,
					Region:   doc.Action.Injection.Region,
				}
			}
			req.Action = act
		} else {
			if *priority < 0 || *hostname == "" || *actionType == "" {
				return fmt.Errorf("provide --file or --priority, --hostname, and --action")
			}
			req.Priority = int32(*priority)
			req.Hostname = *hostname
			act := &jettyv1.RuleAction{Type: *actionType}
			if *actionType == "allow_with_credential" {
				act.Credential = *credential
				act.PathPrefix = *pathPrefix
				act.Injection = &jettyv1.Injection{
					Type:     *injType,
					Username: *injUser,
					Name:     *injHeader,
					Service:  *injService,
					Region:   *injRegion,
				}
			}
			req.Action = act
		}

		if *globalOnly {
			req.VmSelector = &jettyv1.VmSelector{Type: "all"}
		} else if *vmName != "" {
			req.VmSelector = &jettyv1.VmSelector{Type: "only", Vms: []string{*vmName}}
		}
		if req.GetVmSelector() == nil {
			return fmt.Errorf("specify --vm NAME or --global (or vm_selector in --file)")
		}

		resp, err := client.PutRule(ctx, connect.NewRequest(req))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		r := resp.Msg.GetRule()
		scope := r.GetScope()
		if scope == "" {
			scope = "global"
		}
		fmt.Printf("%s/%s (priority %d, %s -> %s)\n", scope, r.GetName(), r.GetPriority(), r.GetHostname(), r.GetAction().GetType())
		return nil

	case "delete":
		name, rest, err := splitLeadingArg(args[1:])
		if err != nil {
			return fmt.Errorf("usage: jetty rule delete NAME [--vm NAME | --global]")
		}
		fs := flag.NewFlagSet("rule delete", flag.ContinueOnError)
		vmName := fs.String("vm", "", "")
		globalOnly := fs.Bool("global", false, "")
		if err := fs.Parse(rest); err != nil {
			return err
		}
		req := &jettyv1.DeleteRuleRequest{Name: name}
		if *globalOnly {
			req.ScopeSpecified = true
			req.Scope = ""
		} else if *vmName != "" {
			req.ScopeSpecified = true
			req.Scope = *vmName
		}
		resp, err := client.DeleteRule(ctx, connect.NewRequest(req))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		fmt.Printf("Deleted rule %s\n", name)
		return nil
	}
	return fmt.Errorf("unknown rule subcommand %q", args[0])
}

func cmdCredential(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: jetty credential <list|get|put|delete|refresh|catalog> ...")
	}
	switch args[0] {
	case "list":
		resp, err := client.ListCredentials(ctx, connect.NewRequest(&jettyv1.ListCredentialsRequest{}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		for _, c := range resp.Msg.GetCredentials() {
			fmt.Printf("%s\t%s\t%ds\n", c.GetName(), c.GetStatus(), c.GetTtlSeconds())
		}
		return nil
	case "get":
		if len(args) < 2 {
			return fmt.Errorf("usage: jetty credential get NAME")
		}
		resp, err := client.GetCredential(ctx, connect.NewRequest(&jettyv1.GetCredentialRequest{Name: args[1]}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		fmt.Printf("%s\tstatus=%s\tttl=%ds\n", resp.Msg.GetName(), resp.Msg.GetStatus(), resp.Msg.GetTtlSeconds())
		return nil
	case "put":
		name, rest, err := splitLeadingArg(args[1:])
		if err != nil {
			return fmt.Errorf("usage: jetty credential put NAME (--command CMD --ttl N | --file cred.json)")
		}
		fs := flag.NewFlagSet("credential put", flag.ContinueOnError)
		cmdStr := fs.String("command", "", "")
		ttl := fs.Int("ttl", 300, "")
		filePath := fs.String("file", "", "")
		if err := fs.Parse(rest); err != nil {
			return err
		}
		finalCmd := *cmdStr
		finalTTL := int32(*ttl)
		if *filePath != "" {
			raw, err := os.ReadFile(*filePath)
			if err != nil {
				return err
			}
			var doc struct {
				Command    string `json:"command"`
				TTLSeconds int32  `json:"ttl_seconds"`
			}
			if err := json.Unmarshal(raw, &doc); err != nil {
				return err
			}
			finalCmd = doc.Command
			finalTTL = doc.TTLSeconds
		}
		resp, err := client.PutCredential(ctx, connect.NewRequest(&jettyv1.PutCredentialRequest{
			Name:       name,
			Command:    finalCmd,
			TtlSeconds: finalTTL,
		}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		c := resp.Msg.GetCredential()
		fmt.Printf("%s\tstatus=%s\tttl=%ds\n", c.GetName(), c.GetStatus(), c.GetTtlSeconds())
		return nil
	case "delete":
		if len(args) < 2 {
			return fmt.Errorf("usage: jetty credential delete NAME")
		}
		resp, err := client.DeleteCredential(ctx, connect.NewRequest(&jettyv1.DeleteCredentialRequest{Name: args[1]}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		fmt.Printf("Deleted credential %s\n", args[1])
		return nil
	case "refresh":
		if len(args) < 2 {
			return fmt.Errorf("usage: jetty credential refresh NAME")
		}
		resp, err := client.RefreshCredential(ctx, connect.NewRequest(&jettyv1.RefreshCredentialRequest{Name: args[1]}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		fmt.Printf("%s\tstatus=%s\n", resp.Msg.GetName(), resp.Msg.GetStatus())
		return nil
	case "catalog":
		resp, err := client.GetQuickAddCatalog(ctx, connect.NewRequest(&jettyv1.Empty{}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		for _, e := range resp.Msg.GetEntries() {
			fmt.Printf("%s\t%s\t%ds\n", e.GetKey(), e.GetDisplayName(), e.GetTtlSeconds())
		}
		return nil
	}
	return fmt.Errorf("unknown credential subcommand %q", args[0])
}

func cmdCA(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) error {
	if len(args) == 0 || args[0] != "show" {
		return fmt.Errorf("usage: jetty ca show")
	}
	resp, err := client.GetCa(ctx, connect.NewRequest(&jettyv1.GetCaRequest{IncludePrivateKey: false}))
	if err != nil {
		return err
	}
	if jsonMode {
		return printProtoJSON(resp.Msg)
	}
	fmt.Printf("Subject: %s\nFingerprint: %s\nValid: %s -> %s\n\n%s",
		resp.Msg.GetSubject(), resp.Msg.GetFingerprintSha256(), resp.Msg.GetNotBefore(), resp.Msg.GetNotAfter(), resp.Msg.GetCertificatePem())
	return nil
}

func cmdLog(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: jetty log <list|show> ...")
	}
	switch args[0] {
	case "list":
		fs := flag.NewFlagSet("log list", flag.ContinueOnError)
		vmName := fs.String("vm", "", "")
		decision := fs.String("decision", "", "")
		host := fs.String("host", "", "")
		limit := fs.Int("limit", 50, "")
		follow := fs.Bool("follow", false, "")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		resp, err := client.ListRequests(ctx, connect.NewRequest(&jettyv1.ListRequestsRequest{
			VmName:   *vmName,
			Decision: *decision,
			Host:     *host,
			Limit:    int32(*limit),
		}))
		if err != nil {
			return err
		}
		if jsonMode && !*follow {
			return printProtoJSON(resp.Msg)
		}
		for _, c := range resp.Msg.GetConnections() {
			printConnLine(c)
		}
		if !*follow {
			return nil
		}
		stream, err := client.TailRequests(ctx, connect.NewRequest(&jettyv1.TailRequestsRequest{
			VmName:   *vmName,
			Decision: *decision,
		}))
		if err != nil {
			return err
		}
		defer stream.Close()
		for stream.Receive() {
			ev := stream.Msg()
			if jsonMode {
				_ = printProtoJSON(ev)
				continue
			}
			if c := ev.GetConnection(); c != nil {
				printConnLine(c)
			}
			if r := ev.GetHttpRequest(); r != nil {
				fmt.Printf("  req #%d (conn #%d)\t%s\t%s\tstatus=%d\t%s\n",
					r.GetId(), r.GetConnectionId(), r.GetMethod(), r.GetPath(), r.GetStatus(), r.GetOutcome())
			}
		}
		return stream.Err()

	case "show":
		if len(args) < 2 {
			return fmt.Errorf("usage: jetty log show ID")
		}
		id, err := strconv.ParseInt(args[1], 10, 64)
		if err != nil {
			return err
		}
		resp, err := client.GetRequest(ctx, connect.NewRequest(&jettyv1.GetRequestRequest{Id: id}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		return printProtoJSON(resp.Msg)
	}
	return fmt.Errorf("unknown log subcommand %q", args[0])
}

func printConnLine(c *jettyv1.ConnectionLog) {
	host := c.GetDestinationHostname()
	if host == "" {
		host = c.GetDestinationIp()
	}
	outcome := c.GetOutcome()
	if c.GetIntercepted() && outcome == "" {
		outcome = "intercepted"
	}
	ts := strings.ReplaceAll(c.GetStartedAt(), "T", " ")
	if len(ts) > 19 {
		ts = ts[:19]
	}
	fmt.Printf("#%d\t%s\t%s\t%s:%d\t%s\n", c.GetId(), ts, c.GetVmName(), host, c.GetDestinationPort(), outcome)
}
