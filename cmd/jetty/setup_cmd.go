package main

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/cau777/jetty-vm/internal/handoff"
)

const daemonUnitName = "jetty-daemon.service"

func quoteSystemdPath(p string) string {
	p = strings.ReplaceAll(p, "%", "%%")
	p = strings.ReplaceAll(p, `\`, `\\`)
	p = strings.ReplaceAll(p, `"`, `\"`)
	return `"` + p + `"`
}

func runSetupCmd() error {
	if os.Geteuid() == 0 {
		return fmt.Errorf("run Jetty setup as your desktop user, not as root")
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	exe, _ = filepath.EvalSymlinks(exe)
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	target := filepath.Join(home, ".local", "bin", "jetty")
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	if exe != target {
		tmp, err := os.CreateTemp(filepath.Dir(target), ".jetty-install-*")
		if err != nil {
			return err
		}
		tmpPath := tmp.Name()
		src, err := os.Open(exe)
		if err != nil {
			tmp.Close()
			os.Remove(tmpPath)
			return err
		}
		_, copyErr := io.Copy(tmp, src)
		src.Close()
		tmp.Close()
		if copyErr != nil {
			os.Remove(tmpPath)
			return copyErr
		}
		if err := os.Chmod(tmpPath, 0o755); err != nil {
			os.Remove(tmpPath)
			return err
		}
		if err := os.Rename(tmpPath, target); err != nil {
			os.Remove(tmpPath)
			return err
		}
	} else {
		_ = os.Chmod(target, 0o755)
	}

	unitDir := filepath.Join(home, ".config", "systemd", "user")
	if err := os.MkdirAll(unitDir, 0o755); err != nil {
		return err
	}
	unit := fmt.Sprintf(
		"# Written by Jetty; rerun `jetty setup` to update it.\n"+
			"[Unit]\nDescription=Jetty management service\nAfter=default.target\n\n"+
			"[Service]\nType=simple\nExecStart=%s daemon\nRestart=on-failure\nRestartSec=5\n\n"+
			"[Install]\nWantedBy=default.target\n",
		quoteSystemdPath(target),
	)
	if err := os.WriteFile(filepath.Join(unitDir, daemonUnitName), []byte(unit), 0o644); err != nil {
		return err
	}

	u, err := user.Current()
	if err == nil {
		if err := exec.Command("loginctl", "enable-linger", u.Username).Run(); err != nil {
			_ = runPkexec("/usr/bin/loginctl", "enable-linger", u.Username)
		}
	}
	if err := exec.Command("systemctl", "--user", "daemon-reload").Run(); err != nil {
		return err
	}
	if err := exec.Command("systemctl", "--user", "enable", "--now", daemonUnitName).Run(); err != nil {
		return err
	}

	relogin, err := bootstrapLXD(u)
	if err != nil {
		return err
	}
	if skillDir, err := handoff.MaterializeSkill(); err == nil {
		if npx, err := exec.LookPath("npx"); err == nil {
			_ = exec.Command(npx, "--yes", "skills", "add", skillDir, "--global", "--copy", "--yes").Run()
		}
	}

	fmt.Printf("Jetty is installed at %s.\n", target)
	if relogin {
		fmt.Println("Sign out and back in so Jetty can access the lxd group. If LXD is still unavailable afterward, reboot so the lingering user manager receives the new group membership.")
	} else {
		fmt.Println("Run `jetty gateway setup` (or open jetty-desktop) to create the gateway and manage VMs.")
	}
	return nil
}

func runPkexec(prog string, args ...string) error {
	pkexec, err := exec.LookPath("pkexec")
	if err != nil {
		return fmt.Errorf("pkexec is required to install and initialize LXD")
	}
	if !filepath.IsAbs(prog) {
		resolved, err := exec.LookPath(prog)
		if err != nil {
			return fmt.Errorf("required program not found: %s", prog)
		}
		prog = resolved
	}
	cmdArgs := append([]string{prog}, args...)
	cmd := exec.Command(pkexec, cmdArgs...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func bootstrapLXD(u *user.User) (bool, error) {
	snap := "/usr/bin/snap"
	if p, err := exec.LookPath("snap"); err == nil {
		snap = p
	}
	if err := exec.Command(snap, "list", "lxd").Run(); err != nil {
		if err := runPkexec(snap, "install", "lxd"); err != nil {
			return false, err
		}
	}
	lxdBin := "/snap/bin/lxd"
	if _, err := os.Stat(lxdBin); err != nil {
		if p, err := exec.LookPath("lxd"); err == nil {
			lxdBin = p
		}
	}
	if err := runPkexec(lxdBin, "init", "--auto"); err != nil {
		return false, err
	}
	grp, err := user.LookupGroup("lxd")
	if err != nil {
		if err := runPkexec("/usr/sbin/groupadd", "--system", "lxd"); err != nil {
			return false, err
		}
		grp, err = user.LookupGroup("lxd")
		if err != nil {
			return false, err
		}
	}
	lxdGID, _ := strconv.Atoi(grp.Gid)
	gids, _ := os.Getgroups()
	gids = append(gids, os.Getgid())
	for _, g := range gids {
		if g == lxdGID {
			return false, nil
		}
	}
	if u != nil {
		if err := runPkexec("/usr/sbin/usermod", "-aG", "lxd", u.Username); err != nil {
			return false, err
		}
	}
	return true, nil
}
