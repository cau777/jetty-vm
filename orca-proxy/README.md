# orca-proxy

The host-side policy proxy for Jetty VMs: Management API, native Qt Quick UI,
rule engine, Credential execution, request logging, and the mitmproxy addon.

## Desktop runtime

The release AppImage contains the `jetty` CLI, daemon, tray, and native
PySide6 Qt Quick (QML) window. `jetty daemon` serves the loopback API as soon
as the user service starts, then starts and supervises mitmproxy when the Jetty
uplink address exists. `jetty tray` polls the daemon and renders the management
UI with QML. The tray and daemon are separate processes so a desktop failure
does not stop VM web egress. All UI HTTP requests run in worker threads.

VM creation and gateway setup run as daemon jobs. The API returns a job ID and
reports real provisioning stages from `GET /api/v1/jetty/jobs/{job_id}`. The
CLI follows the same job until completion, so its commands remain blocking.

LXD operations use its local Unix-socket REST API. Jetty manages the gateway,
agent VMs, and the generated `~/.ssh/jetty_config` fragment. Local membership
in the `lxd` group is root-equivalent on the host.

Port Forwards expose a VM port on the host's `127.0.0.1`, for example a dev
server. The daemon supervises an `ssh -N -L` per forward through the gateway's
per-VM SSH port, so services bound to the VM's localhost are reachable too. It
uses its own key in `~/.orca-proxy/port-forwards/`, authorized on the VM and
checked against the VM's host key over the LXD socket. One-time forwards last
until closed or the daemon stops; persistent ones are stored and reopen when
the daemon starts. Deleting a VM removes its forwards.

Reverse Forwards go the other way: they expose a port on the host's localhost
on the VM's `127.0.0.1`, for example a database or a local model server. The
daemon supervises an `ssh -N -R` over the same connection, so the gateway's
firewall still drops VM traffic to host and LAN addresses. VM ports start at
1024 because the VM side binds as `ubuntu`. Several VMs may reach the same host
port. The agent gets whatever access that service grants, so expose only
services you would hand it.

## CLI

```bash
jetty status
jetty gateway setup
jetty vm create my-agent --cpus 4 --memory 8GiB --disk 40GiB
jetty vm exec my-agent -- uname -a
jetty vm upload my-agent /home/ubuntu/setup.sh --file ./setup.sh
jetty vm port open my-agent 5173                 # one-time, localhost:5173
jetty vm port open my-agent 8000 --host-port 18000 --persistent
jetty vm port list
jetty vm port close my-agent 5173
jetty vm reverse-port open my-agent 5432         # VM localhost:5432 -> host localhost:5432
jetty vm reverse-port open my-agent 11434 --vm-port 21434 --persistent
jetty vm reverse-port list
jetty vm reverse-port close my-agent 5432        # by VM port
```

Run `jetty --help` and `jetty vm --help` for the other lifecycle operations.

## AWS CLI authentication

Create a Credential whose bash command prints AWS process-format JSON. The
desktop's **AWS host profile** Quick Add template starts with:

```bash
aws configure export-credentials --profile default --format process
```

Change `default` to a dedicated host profile with the IAM permissions the VM
needs. Acquisition and provider refresh stay in that bash command; any required
interactive login happens on the host. A custom command can emit the same
[AWS process credential format](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sourcing-external.html):
`Version: 1`, `AccessKeyId`, `SecretAccessKey`, optional `SessionToken`, and
optional timezone-qualified `Expiration`. Pretty-printed JSON is accepted for
AWS injection. The value stays in memory, cached for the configured TTL or
until 30 seconds before expiration, whichever is sooner. TTL zero disables
caching. Bearer and Basic commands still require a single-line value.

Create an Allow-with-credential rule for the VM and exact AWS endpoint. Choose
**AWS SigV4** in the rule editor and specify its signing service and region.
For example, an STS rule action is:

```json
{
  "type": "allow_with_credential",
  "credential": "aws-host-profile",
  "path_prefix": "/",
  "injection": {"type": "aws_sigv4", "service": "sts", "region": "us-east-1"}
}
```

Use `sts.us-east-1.amazonaws.com` as that rule's hostname. Configure AWS CLI
inside the VM with dummy credentials and the system bundle containing Jetty's
public CA:

```bash
export AWS_ACCESS_KEY_ID=jetty-placeholder
export AWS_SECRET_ACCESS_KEY=jetty-placeholder
export AWS_DEFAULT_REGION=us-east-1
export AWS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
export AWS_EC2_METADATA_DISABLED=true
unset AWS_SESSION_TOKEN
aws sts get-caller-identity --endpoint-url https://sts.us-east-1.amazonaws.com
```

The proxy replaces guest authentication, recomputes the payload hash, and
signs the outgoing request using the rule's service and region. Add rules for
each endpoint needed, including S3 bucket endpoints and redirect destinations.
IAM controls allowed operations and resources: many AWS APIs share the same
hostname and `/` path. Avoid IAM permissions that allow the VM to mint or
retrieve further credentials if those must also stay outside the VM.

Requests are buffered for signing. AWS streaming uploads (`aws-chunked` or
`STREAMING-*` payload hashes), SigV4a, and presigned request URLs receive a
proxy error. For ordinary S3 uploads, setting
`AWS_REQUEST_CHECKSUM_CALCULATION=when_required` in the VM avoids optional
checksum trailers; explicitly requested or required trailers remain unsupported.
See [AWS checksum configuration](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html).
Generating a presigned URL locally with dummy VM credentials produces an
unusable URL because there is no outgoing request for the proxy to sign.

## Development

```bash
uv sync
uv run python -m orca_proxy   # Management API on loopback
```

Set `ORCA_PROXY_HOME` to override the data directory (used by tests to isolate
each run in a temporary directory). The desktop release is built with
`release/jetty.spec` and `.github/workflows/release.yml`.

`tests/e2e/lxd-gateway-checks.sh` checks the full topology against real VMs
(identity, policy, spoofing, isolation, and fail-closed behavior); see its
header for how to run it against a test proxy.
