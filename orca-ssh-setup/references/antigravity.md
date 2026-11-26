# Google Antigravity CLI on a Jetty VM

Use this procedure when Antigravity CLI (`agy`) was requested. Tested with
agy 1.5.16 on Ubuntu, using a consumer subscription. Desktop/IDE logins,
Gemini API keys, and enterprise ADC are different authentication modes.
The official [installer](https://antigravity.google/docs/cli/tutorial) and
[CLI reference](https://antigravity.google/docs/cli/reference.md) describe
installation and headless flags; the placeholder setup below is Jetty-specific.

## Host credential

Register `antigravity-subscription` from the shipped credential catalog using
step 3c. The command uses `/usr/bin/python3` with `python3-dbus`, and the
host desktop Secret Service session bus. On Ubuntu, install `python3-dbus`
on the host if missing. The daemon must run as the desktop user with access
to that user's unlocked keyring; do not run it as root or unlock the keyring
with a password stored in a script.

Before registering the Credential, have the host owner put the OAuth client
ID and secret that issued the Antigravity consumer session in
`antigravity-oauth-client.json` in Jetty's host data directory (`ORCA_PROXY_HOME`
or `~/.orca-proxy`). Create the file with mode `0600`. Use the following shape
and keep the actual values on the host.

```json
{"client_id": "<client ID>", "client_secret": "<client secret>"}
```

The Linux CLI stores a JSON session in Secret Service under
`service=gemini`, `username=antigravity`. The template refreshes that session
at `https://oauth2.googleapis.com/token`, updates the existing keyring item,
and prints only the access token for the proxy's in-memory cache. It keeps
account metadata and retains the old refresh token when Google omits a new
one. These storage details can change with CLI releases. Recheck them if an
upgrade breaks refresh.
The template does not support a host using forced file storage or another OS.

If refresh fails, inspect Credential status, the host OAuth client configuration,
and the host `agy` login. Keep the real keyring entry, refresh token, and access
token on the host.

## Guest install and Rules

Inspect existing installation first. If absent, install with the official
installer inside the selected VM:

```bash
"$JETTY" vm exec <vm-name> -- bash -lc \
  'curl -fsSL https://antigravity.google/cli/install.sh | bash'
"$JETTY" vm exec <vm-name> -- /home/ubuntu/.local/bin/agy --version
```

Use the VM's actual home directory if it differs. Ensure `~/.local/bin` is
on the guest PATH for interactive use.

Create VM-scoped Allow-with-credential Rules using the step 6 API pattern,
with credential `antigravity-subscription` and bearer injection:

| Hostname | Path prefix | Purpose |
| --- | --- | --- |
| `daily-cloudcode-pa.googleapis.com` | `/` | Consumer configuration, model discovery, streaming inference |
| `www.googleapis.com` | `/oauth2/v2/userinfo` | CLI user identity check |

`aicode.googleapis.com` telemetry calls needed no injection in the verified
CLI version. Add `cloudcode-pa.googleapis.com` only if the installed release
actually sends its authenticated service requests there.

Assign unused priorities after inspecting existing VM and Global Rules. Do
not create wildcard `*.googleapis.com` injection. Check proxy request metadata
if a later CLI version uses different endpoints before adding a new Rule.
The host refresh request goes directly to Google; the guest needs no Rule
for `oauth2.googleapis.com` and has no real token to refresh.

## Guest placeholder session

Consumer mode needs a native session before sending service requests. Create
this **dummy** file in the guest only if it has no existing real session;
if a real login is present, establish how to remove it before replacing it.
The long expiry prevents the guest from trying to refresh a dummy token.

```bash
"$JETTY" vm exec <vm-name> -- bash -lc 'umask 077
mkdir -p ~/.gemini/antigravity-cli
cat > ~/.gemini/antigravity-cli/antigravity-oauth-token <<"JSON"
{"token":{"access_token":"placeholder","token_type":"Bearer","expiry":"2099-01-01T00:00:00Z"},"auth_method":"consumer","id_token_email":"placeholder@example.invalid"}
JSON
'
```

Set `AGY_CLI_FORCE_FILE_TOKEN_STORAGE=1` for guest `agy` invocations (persist
in the guest shell environment if requested). This selects the dummy file
instead of a desktop keyring. Keep provider endpoints at their defaults.
Do not set `AGY_ADC_AUTH`, `GEMINI_API_KEY`, or real OAuth credentials.

## Verify

Run model discovery and then an actual inference turn; discovery alone does
not exercise the inference endpoint:

```bash
"$JETTY" vm exec <vm-name> -- bash -lc \
  'AGY_CLI_FORCE_FILE_TOKEN_STORAGE=1 ~/.local/bin/agy models'
"$JETTY" vm exec <vm-name> -- bash -lc \
  'cd /tmp && AGY_CLI_FORCE_FILE_TOKEN_STORAGE=1 ~/.local/bin/agy -p "Reply with exactly ANTIGRAVITY_OK. Do not read files or run tools." --print-timeout 60s'
```

Check the proxy's request records for the selected VM: relevant requests
must match its Antigravity Rules and return successful upstream responses.
Check Credential status is `valid` and that the guest session still contains
only `placeholder`. Never print the host Credential Value during validation.
A 401 with default-Allow in the request log means no injection Rule matched;
a Credential execution error indicates host keyring/refresh trouble.
