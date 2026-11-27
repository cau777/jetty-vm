// Package aws implements AWS endpoint selection, process-credential parsing, and SigV4 signing.
package aws

import (
	"bytes"
	"compress/gzip"
	_ "embed"
	"encoding/json"
	"strings"
	"sync"
)

//go:embed endpoints_catalog.json.gz
var endpointsCatalogGZ []byte

type endpointCatalog struct {
	Regions   []string            `json:"regions"`
	Endpoints map[string][]string `json:"endpoints"`
}

// Scope identifies an AWS SigV4 signing service and region.
type Scope struct {
	Service string
	Region  string
}

var (
	catalogOnce sync.Once
	knownRegions map[string]struct{}
	endpointsMap map[string]Scope
)

func loadCatalog() {
	catalogOnce.Do(func() {
		r, err := gzip.NewReader(bytes.NewReader(endpointsCatalogGZ))
		if err != nil {
			panic(err)
		}
		defer r.Close()
		var cat endpointCatalog
		if err := json.NewDecoder(r).Decode(&cat); err != nil {
			panic(err)
		}
		knownRegions = make(map[string]struct{}, len(cat.Regions))
		for _, reg := range cat.Regions {
			knownRegions[reg] = struct{}{}
		}
		endpointsMap = make(map[string]Scope, len(cat.Endpoints))
		for host, pair := range cat.Endpoints {
			if len(pair) == 2 {
				endpointsMap[host] = Scope{Service: pair[0], Region: pair[1]}
			}
		}
	})
}

// SigningScope returns the supported AWS API's (service, region) signing scope, or ok=false.
// Mirrors Python aws_endpoints.signing_scope().
func SigningScope(hostname string) (Scope, bool) {
	loadCatalog()
	hostname = strings.TrimRight(strings.ToLower(hostname), ".")
	if hostname == "" {
		return Scope{}, false
	}
	if scope, ok := endpointsMap[hostname]; ok {
		return scope, true
	}
	// Resource subdomains are supported only for s3 and execute-api, matching aws_endpoints.py.
	idx := 0
	for {
		dot := strings.IndexByte(hostname[idx:], '.')
		if dot < 0 {
			break
		}
		parent := hostname[idx+dot+1:]
		if scope, ok := endpointsMap[parent]; ok && (scope.Service == "s3" || scope.Service == "execute-api") {
			return scope, true
		}
		idx += dot + 1
	}
	return Scope{}, false
}
