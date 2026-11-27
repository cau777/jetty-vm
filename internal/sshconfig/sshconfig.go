// Package sshconfig writes Jetty's ~/.ssh/jetty_config fragment without touching user blocks.
package sshconfig

import (
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

var hostOrMatchRe = regexp.MustCompile(`(?i)^\s*(?:Host|Match)\s+`)

func atomicWrite(path, content string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := filepath.Join(filepath.Dir(path), "."+filepath.Base(path)+".tmp")
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.WriteString(content); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return err
	}
	return nil
}

// WriteJettyConfig writes ~/.ssh/jetty_config and prepends Include jetty_config to ~/.ssh/config if absent.
func WriteJettyConfig(fragment, sshDir string) (string, error) {
	if sshDir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		sshDir = filepath.Join(home, ".ssh")
	}
	if err := os.MkdirAll(sshDir, 0o700); err != nil {
		return "", err
	}
	fragmentPath := filepath.Join(sshDir, "jetty_config")
	configPath := filepath.Join(sshDir, "config")
	if err := atomicWrite(fragmentPath, strings.TrimRight(fragment, "\r\n")+"\n"); err != nil {
		return "", err
	}

	raw, err := os.ReadFile(configPath)
	existing := ""
	if err == nil {
		existing = string(raw)
	}
	hasGlobalInclude := false
	for _, line := range strings.Split(existing, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		if hostOrMatchRe.MatchString(line) {
			break
		}
		fields := strings.Fields(trimmed)
		if len(fields) >= 2 && strings.ToLower(fields[0]) == "include" {
			for _, pat := range fields[1:] {
				pat = strings.Trim(pat, `"'`)
				if filepath.Base(pat) == "jetty_config" {
					hasGlobalInclude = true
					break
				}
			}
			if hasGlobalInclude {
				break
			}
		}
	}
	if !hasGlobalInclude {
		prefix := "Include jetty_config\n"
		if existing != "" && !strings.HasPrefix(existing, "\n") {
			prefix += "\n"
		}
		if err := atomicWrite(configPath, prefix+existing); err != nil {
			return "", err
		}
	} else if _, err := os.Stat(configPath); err == nil {
		_ = os.Chmod(configPath, 0o600)
	}
	return fragmentPath, nil
}
