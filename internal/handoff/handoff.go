// Package handoff launches a host terminal with a coding agent targeting a Jetty VM.
package handoff

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"sort"
	"strings"
	"time"

	jettyvm "github.com/cau777/jetty-vm"
	jerr "github.com/cau777/jetty-vm/internal/errors"
)

const SkillName = "orca-ssh-setup"

// Agent describes a supported host coding-agent harness.
type Agent struct {
	Label     string
	SkillsID  string
	Command   []string
	SkillDirs []string
}

var Agents = map[string]Agent{
	"claude": {
		Label:     "Claude Code",
		SkillsID:  "claude-code",
		Command:   []string{"claude"},
		SkillDirs: []string{".claude/skills"},
	},
	"codex": {
		Label:     "Codex",
		SkillsID:  "codex",
		Command:   []string{"codex"},
		SkillDirs: []string{".codex/skills", ".agents/skills"},
	},
	"pi": {
		Label:     "Pi",
		SkillsID:  "pi",
		Command:   []string{"pi"},
		SkillDirs: []string{".pi/agent/skills"},
	},
	"opencode": {
		Label:     "OpenCode",
		SkillsID:  "opencode",
		Command:   []string{"opencode", "--prompt"},
		SkillDirs: []string{".config/opencode/skills", ".agents/skills"},
	},
}

type terminalDef struct {
	name   string
	prefix []string
}

var terminals = []terminalDef{
	{"ptyxis", []string{"--"}},
	{"gnome-terminal", []string{"--"}},
	{"kgx", []string{"--"}},
	{"konsole", []string{"-e"}},
	{"xfce4-terminal", []string{"-x"}},
	{"kitty", nil},
	{"alacritty", []string{"-e"}},
	{"wezterm", []string{"start", "--"}},
	{"x-terminal-emulator", []string{"-e"}},
}

const handoffScript = `
cd "$1" || { echo "Cannot open $1"; read -r _; exit 1; }
export JETTY_CLI="$5"
if [ "$2" = 1 ]; then
  echo "Installing the orca-ssh-setup skill for this agent..."
  if ! npx --yes skills add "$3" --global --agent "$4" --copy --yes; then
    echo "The skill could not be installed. Install orca-ssh-setup from the Jetty repository."
    printf "Press Enter to start the agent anyway..."; read -r _
  fi
fi
shift 5
if ! command -v "$1" >/dev/null 2>&1; then
  echo "$1 is not installed or not on PATH."
  printf "Press Enter to close..."; read -r _
  exit 1
fi
exec "$@"
`

// Result summarizes a launched handoff session.
type Result struct {
	Agent        string
	Label        string
	SkillInstall bool
	Terminal     string
	ProjectDir   string
	Jetty        string
}

func treeDigestDir(root string) string {
	var files []string
	_ = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			files = append(files, p)
		}
		return nil
	})
	sort.Strings(files)
	h := sha256.New()
	for _, p := range files {
		rel, _ := filepath.Rel(root, p)
		b, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		h.Write([]byte(filepath.ToSlash(rel)))
		h.Write([]byte{0})
		h.Write(b)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// MaterializeSkill writes the embedded orca-ssh-setup skill to ~/.local/share/jetty/orca-ssh-setup.
func MaterializeSkill() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	target := filepath.Join(home, ".local", "share", "jetty", SkillName)
	for _, rel := range []string{"SKILL.md", "references/antigravity.md"} {
		data, err := fs.ReadFile(jettyvm.Assets, SkillName+"/"+rel)
		if err != nil {
			return "", err
		}
		dst := filepath.Join(target, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
			return "", err
		}
		if err := os.WriteFile(dst, data, 0o644); err != nil {
			return "", err
		}
	}
	return target, nil
}

func skillCurrent(agent Agent, skillDir string) bool {
	home, err := os.UserHomeDir()
	if err != nil {
		return false
	}
	expected := treeDigestDir(skillDir)
	for _, d := range agent.SkillDirs {
		installed := filepath.Join(home, filepath.FromSlash(d), SkillName)
		if _, err := os.Stat(filepath.Join(installed, "SKILL.md")); err == nil {
			if treeDigestDir(installed) == expected {
				return true
			}
		}
	}
	return false
}

func jettyExecutable() string {
	if exe, err := os.Executable(); err == nil && exe != "" {
		return exe
	}
	if p, err := exec.LookPath("jetty"); err == nil {
		return p
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".local", "bin", "jetty")
}

func findTerminal() (string, []string, error) {
	for _, t := range terminals {
		if p, err := exec.LookPath(t.name); err == nil {
			return p, t.prefix, nil
		}
	}
	return "", nil, jerr.ServiceUnavailable("No supported terminal emulator was found")
}

func loginShell() string {
	shell := os.Getenv("SHELL")
	if shell == "" {
		if u, err := user.Current(); err == nil {
			shell = fmt.Sprintf("/bin/bash")
			_ = u
		}
	}
	base := filepath.Base(shell)
	if (base == "bash" || base == "zsh") && shell != "" {
		if _, err := os.Stat(shell); err == nil {
			return shell
		}
	}
	if p, err := exec.LookPath("bash"); err == nil {
		return p
	}
	return "/bin/bash"
}

func shellQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "'\"'\"'") + "'"
}

func buildPrompt(vmName, ipAddress, jettyBin string) string {
	return fmt.Sprintf(
		"Use the orca-ssh-setup skill to help manage the existing Jetty VM '%s' "+
			"(address %s, SSH host alias %s). Jetty's CLI is %s (also exported as $JETTY_CLI). "+
			"You are in a temporary host folder, independent of any repository. "+
			"Start by asking what the user wants to do with this VM, for example configure a repo, "+
			"install Docker, or increase CPU allocation. Wait for their answer before making changes. "+
			"Then confirm the VM with `%s vm list` and inspect only what the task needs. "+
			"Reuse this VM and existing checkouts; clone a repository only when the requested task needs one.",
		vmName, ipAddress, vmName, jettyBin, shellQuote(jettyBin),
	)
}

// Launch opens a host terminal with the selected agent targeting vmName.
func Launch(agentKey, vmName, ipAddress string) (Result, error) {
	agent, ok := Agents[agentKey]
	if !ok {
		return Result{}, jerr.Validation("agent must be one of: claude, codex, pi, opencode", map[string]string{"agent": "unsupported agent"})
	}
	skillDir, err := MaterializeSkill()
	if err != nil {
		return Result{}, jerr.ServiceUnavailable(err.Error())
	}
	install := !skillCurrent(agent, skillDir)
	terminal, prefix, err := findTerminal()
	if err != nil {
		return Result{}, err
	}
	jettyBin := jettyExecutable()
	workspace, err := os.MkdirTemp("", "jetty-handoff-")
	if err != nil {
		return Result{}, err
	}
	installFlag := "0"
	if install {
		installFlag = "1"
	}
	shellArgs := []string{
		"-lic", handoffScript, "jetty-handoff",
		workspace, installFlag, skillDir, agent.SkillsID, jettyBin,
	}
	shellArgs = append(shellArgs, agent.Command...)
	shellArgs = append(shellArgs, buildPrompt(vmName, ipAddress, jettyBin))

	var cmdArgs []string
	cmdArgs = append(cmdArgs, prefix...)
	cmdArgs = append(cmdArgs, loginShell())
	cmdArgs = append(cmdArgs, shellArgs...)

	execBin := terminal
	execArgs := cmdArgs
	if sr, err := exec.LookPath("systemd-run"); err == nil {
		rnd := make([]byte, 4)
		_, _ = rand.Read(rnd)
		execBin = sr
		execArgs = append([]string{
			"--user", "--collect", "--quiet",
			"--unit=jetty-handoff-" + hex.EncodeToString(rnd),
			"--working-directory=" + workspace,
			terminal,
		}, cmdArgs...)
	}

	cmd := exec.Command(execBin, execArgs...)
	cmd.Dir = workspace
	done := make(chan error, 1)
	go func() {
		_, err := cmd.CombinedOutput()
		done <- err
	}()
	select {
	case err := <-done:
		if err != nil {
			_ = os.RemoveAll(workspace)
			return Result{}, jerr.ServiceUnavailable(fmt.Sprintf("Could not open a terminal: %v", err))
		}
	case <-time.After(15 * time.Second):
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
		_ = os.RemoveAll(workspace)
		return Result{}, jerr.ServiceUnavailable("Could not open a terminal: timed out")
	}

	return Result{
		Agent:        agentKey,
		Label:        agent.Label,
		SkillInstall: install,
		Terminal:     filepath.Base(terminal),
		ProjectDir:   workspace,
		Jetty:        jettyBin,
	}, nil
}
