#!/usr/bin/env python3
# gh-rest.py — a drop-in replacement for the `gh` CLI that speaks only the
# GitHub REST API (never GraphQL). Meant to be installed as /usr/local/bin/gh
# inside orca-proxy-fronted VMs: orca-proxy's Rules only allow-list specific
# REST path prefixes on api.github.com, so any command the real `gh` binary
# routes through GraphQL (gh pr create, gh run watch, ...) 401s/403s there
# even with a valid credential. This script implements the same subcommands
# against REST endpoints only, so every `gh` call an agent makes is one the
# proxy can actually authorize.
#
# Auth: reads GH_TOKEN, falling back to GITHUB_TOKEN (same precedence as the
# real gh CLI). No token is required to run — requests are still sent with a
# placeholder Authorization header so that a credential-injecting proxy in
# front of api.github.com has something to overwrite.
#
# Known REST gaps (GitHub exposes these only via GraphQL, so there is no
# faithful REST implementation): `pr merge --auto` (enabling auto-merge) and
# `pr ready` (marking a draft PR ready for review). Both fail loudly with a
# clear explanation instead of silently no-op'ing.
#
# --jq shells out to the system `jq` binary (apt-get install -y jq) rather
# than reimplementing jq's expression language.

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"


class GhError(Exception):
    pass


def token() -> str:
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""


def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)


# --------------------------------------------------------------------------
# repo detection
# --------------------------------------------------------------------------

def detect_repo() -> tuple[str, str]:
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
        )
    except Exception as exc:
        raise GhError(
            "could not determine repo: not in a git checkout with a GitHub "
            "'origin' remote (pass -R owner/repo)"
        ) from exc
    url = proc.stdout.strip()
    m = re.search(r"github\.com[:/]+([^/]+)/(.+?)(\.git)?$", url)
    if not m:
        raise GhError(f"origin remote '{url}' is not a github.com URL (pass -R owner/repo)")
    return m.group(1), m.group(2)


def resolve_repo(repo_flag: str | None) -> tuple[str, str]:
    if repo_flag:
        if "/" not in repo_flag:
            raise GhError("-R/--repo must be OWNER/REPO")
        owner, repo = repo_flag.split("/", 1)
        return owner, repo
    return detect_repo()


def current_branch() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        segs = part.split(";")
        if len(segs) < 2:
            continue
        url_part = segs[0].strip().lstrip("<").rstrip(">")
        if segs[1].strip() == 'rel="next"':
            return url_part
    return None


def request(
    method: str,
    path: str,
    params: dict | None = None,
    body: object | None = None,
    headers: dict | None = None,
    raw: bool = False,
    paginate: bool = False,
):
    """Issue one REST call (or, with paginate=True, follow every Link: next
    page and merge array results together). Returns parsed JSON, raw bytes
    (raw=True), or a merged list (paginate=True on a list-returning endpoint).
    """
    url = path if path.startswith("http://") or path.startswith("https://") else API_ROOT + "/" + path.lstrip("/")
    if params:
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        if qs:
            url += ("&" if "?" in url else "?") + qs

    hdrs = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "gh-rest.py",
    }
    # Only set Authorization when we actually have something to send: a
    # credential-injecting proxy overwrites this header on matched paths
    # regardless of whether it was present, and omitting it on unmatched
    # paths lets anonymous public-repo reads succeed instead of 401'ing on
    # a placeholder Bearer value.
    tok = token()
    if tok:
        hdrs["Authorization"] = f"Bearer {tok}"
    if headers:
        hdrs.update(headers)

    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")

    merged: list = []
    while True:
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                raw_body = resp.read()
                link = resp.headers.get("Link", "")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace")
            try:
                msg = json.loads(err_body).get("message", err_body)
            except Exception:
                msg = err_body
            raise GhError(f"HTTP {e.code}: {msg}")
        except urllib.error.URLError as e:
            raise GhError(f"network error: {e.reason}")

        if raw:
            return raw_body

        parsed = json.loads(raw_body) if raw_body.strip() else None

        if not paginate:
            return parsed

        if isinstance(parsed, list):
            merged.extend(parsed)
        elif isinstance(parsed, dict):
            # Endpoints like {"total_count":N,"jobs":[...]} / {"workflows":[...]}:
            # merge the sole list-valued field across pages.
            list_fields = [k for k, v in parsed.items() if isinstance(v, list)]
            if len(list_fields) == 1:
                merged.extend(parsed[list_fields[0]])
            else:
                return parsed
        else:
            return parsed

        nxt = _next_link(link)
        if not nxt:
            return merged
        url, data, method = nxt, None, "GET"


def paged_list(path: str, params: dict | None = None) -> list:
    """GET an endpoint that returns a plain JSON array, following the Link:
    rel="next" header rather than incrementing ?page= ourselves — some
    large/old GitHub datasets (e.g. very active issue trackers) reject
    offset-style ?page= pagination outright and require this instead."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    result = request("GET", path, params=params, paginate=True)
    return result if isinstance(result, list) else []


# --------------------------------------------------------------------------
# jq passthrough
# --------------------------------------------------------------------------

def run_jq(data, filter_expr: str) -> None:
    if shutil.which("jq") is None:
        raise GhError("--jq needs the 'jq' binary; install it first (apt-get install -y jq)")
    proc = subprocess.run(
        ["jq", "-r", filter_expr],
        input=json.dumps(data).encode(),
        capture_output=True,
    )
    sys.stdout.write(proc.stdout.decode())
    if proc.returncode != 0:
        eprint(proc.stderr.decode().strip())
        raise SystemExit(proc.returncode)


def emit(data, jq_filter: str | None):
    if jq_filter:
        run_jq(data, jq_filter)
    elif isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2))
    elif data is not None:
        print(data)


def emit_json_fields(obj: dict, fields: str | None):
    if fields:
        wanted = [f.strip() for f in fields.split(",") if f.strip()]
        print(json.dumps({k: obj.get(k) for k in wanted}, indent=2))
    else:
        print(json.dumps(obj, indent=2))


# --------------------------------------------------------------------------
# shared arg helpers
# --------------------------------------------------------------------------

def add_repo_flag(p: argparse.ArgumentParser):
    p.add_argument("-R", "--repo", dest="repo", help="OWNER/REPO (default: detected from origin remote)")


def read_body(body: str | None, body_file: str | None) -> str | None:
    if body_file:
        if body_file == "-":
            return sys.stdin.read()
        with open(body_file) as fh:
            return fh.read()
    return body


def split_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


# --------------------------------------------------------------------------
# pr
# --------------------------------------------------------------------------

def last_commit_subject_body() -> tuple[str, str]:
    proc = subprocess.run(
        ["git", "log", "-1", "--pretty=%s%n%n%b"],
        capture_output=True, text=True, check=True,
    )
    text = proc.stdout.strip("\n")
    subject, _, rest = text.partition("\n")
    return subject.strip(), rest.strip()


def cmd_pr_create(args):
    owner, repo = resolve_repo(args.repo)
    title, body = args.title, read_body(args.body, args.body_file)
    if args.fill and not title:
        title, filled_body = last_commit_subject_body()
        body = body or filled_body
    if not title:
        raise GhError("--title (or --fill) is required")
    head = args.head or current_branch()
    payload = {"title": title, "head": head, "base": args.base or "main", "body": body or "", "draft": bool(args.draft)}
    pr = request("POST", f"repos/{owner}/{repo}/pulls", body=payload)
    number = pr["number"]

    labels = split_list(args.label)
    if labels:
        request("POST", f"repos/{owner}/{repo}/issues/{number}/labels", body={"labels": labels})
    assignees = split_list(args.assignee)
    if assignees:
        request("POST", f"repos/{owner}/{repo}/issues/{number}/assignees", body={"assignees": assignees})
    reviewers = split_list(args.reviewer)
    if reviewers:
        request("POST", f"repos/{owner}/{repo}/pulls/{number}/requested_reviewers", body={"reviewers": reviewers})

    print(pr["html_url"])
    if args.web:
        print("(--web: open the URL above manually; this host has no browser)")


def cmd_pr_list(args):
    owner, repo = resolve_repo(args.repo)
    if args.author or args.label:
        q = [f"repo:{owner}/{repo}", "type:pr", f"state:{args.state}"]
        if args.author:
            q.append(f"author:{args.author}")
        for lbl in split_list(args.label):
            q.append(f'label:"{lbl}"')
        if args.base:
            q.append(f"base:{args.base}")
        result = request("GET", "search/issues", params={"q": " ".join(q), "per_page": args.limit})
        items = result.get("items", [])
    else:
        items = paged_list(
            f"repos/{owner}/{repo}/pulls",
            params={"state": args.state, "base": args.base},
        )
        items = items[: args.limit]
    for it in items:
        print(f"#{it['number']}\t{it['title']}\t{it.get('head', {}).get('ref', '')}\t{it['state']}")


def cmd_pr_view(args):
    owner, repo = resolve_repo(args.repo)
    pr = request("GET", f"repos/{owner}/{repo}/pulls/{args.number}")
    if args.comments:
        comments = paged_list(f"repos/{owner}/{repo}/issues/{args.number}/comments")
        if args.json is not None:
            print(json.dumps(comments, indent=2))
        else:
            for c in comments:
                print(f"--- {c['user']['login']} ---\n{c['body']}\n")
        return
    if args.json is not None:
        emit_json_fields(pr, args.json)
        return
    print(f"#{pr['number']} {pr['title']} [{pr['state']}]")
    print(f"{pr['head']['ref']} -> {pr['base']['ref']}")
    print(pr["html_url"])
    if pr.get("body"):
        print(f"\n{pr['body']}")
    if args.web:
        print("(--web: open the URL above manually; this host has no browser)")


def cmd_pr_diff(args):
    owner, repo = resolve_repo(args.repo)
    diff = request(
        "GET", f"repos/{owner}/{repo}/pulls/{args.number}",
        headers={"Accept": "application/vnd.github.v3.diff"}, raw=True,
    )
    sys.stdout.write(diff.decode(errors="replace"))


def cmd_pr_checkout(args):
    branch = f"pr-{args.number}"
    subprocess.run(["git", "fetch", "origin", f"pull/{args.number}/head:{branch}"], check=True)
    subprocess.run(["git", "checkout", branch], check=True)


def cmd_pr_merge(args):
    owner, repo = resolve_repo(args.repo)
    if args.auto:
        raise GhError(
            "auto-merge has no REST endpoint — GitHub only exposes it via the "
            "GraphQL enablePullRequestAutoMerge mutation, which this proxy setup "
            "cannot reach. Merge manually with --squash/--merge/--rebase instead."
        )
    method = "squash" if args.squash else "rebase" if args.rebase else "merge"
    result = request("PUT", f"repos/{owner}/{repo}/pulls/{args.number}/merge", body={"merge_method": method})
    print(result.get("message", "merged"))
    if args.delete_branch:
        pr = request("GET", f"repos/{owner}/{repo}/pulls/{args.number}")
        ref = pr["head"]["ref"]
        request("DELETE", f"repos/{owner}/{repo}/git/refs/heads/{ref}")
        print(f"deleted branch {ref}")


def cmd_pr_review(args):
    owner, repo = resolve_repo(args.repo)
    event = "APPROVE" if args.approve else "REQUEST_CHANGES" if args.request_changes else "COMMENT"
    body = read_body(args.body, None) or ""
    request("POST", f"repos/{owner}/{repo}/pulls/{args.number}/reviews", body={"event": event, "body": body})
    print(f"submitted {event.lower()} review on #{args.number}")


def cmd_pr_comment(args):
    owner, repo = resolve_repo(args.repo)
    body = read_body(args.body, args.body_file)
    if not body:
        raise GhError("--body or --body-file is required")
    c = request("POST", f"repos/{owner}/{repo}/issues/{args.number}/comments", body={"body": body})
    print(c["html_url"])


def cmd_pr_edit(args):
    owner, repo = resolve_repo(args.repo)
    payload = {}
    if args.title:
        payload["title"] = args.title
    if args.body is not None or args.body_file:
        payload["body"] = read_body(args.body, args.body_file)
    if args.base:
        payload["base"] = args.base
    if payload:
        request("PATCH", f"repos/{owner}/{repo}/pulls/{args.number}", body=payload)
    add_labels = split_list(args.add_label)
    if add_labels:
        request("POST", f"repos/{owner}/{repo}/issues/{args.number}/labels", body={"labels": add_labels})
    for lbl in split_list(args.remove_label):
        request("DELETE", f"repos/{owner}/{repo}/issues/{args.number}/labels/{urllib.parse.quote(lbl)}")
    add_assignees = split_list(args.add_assignee)
    if add_assignees:
        request("POST", f"repos/{owner}/{repo}/issues/{args.number}/assignees", body={"assignees": add_assignees})
    remove_assignees = split_list(args.remove_assignee)
    if remove_assignees:
        request("DELETE", f"repos/{owner}/{repo}/issues/{args.number}/assignees", body={"assignees": remove_assignees})
    add_reviewers = split_list(args.add_reviewer)
    if add_reviewers:
        request("POST", f"repos/{owner}/{repo}/pulls/{args.number}/requested_reviewers", body={"reviewers": add_reviewers})
    print(f"updated #{args.number}")


def cmd_pr_close(args):
    owner, repo = resolve_repo(args.repo)
    request("PATCH", f"repos/{owner}/{repo}/pulls/{args.number}", body={"state": "closed"})
    print(f"closed #{args.number}")


def cmd_pr_reopen(args):
    owner, repo = resolve_repo(args.repo)
    request("PATCH", f"repos/{owner}/{repo}/pulls/{args.number}", body={"state": "open"})
    print(f"reopened #{args.number}")


def cmd_pr_ready(args):
    raise GhError(
        "marking a PR ready for review has no REST endpoint — GitHub only "
        "exposes it via the GraphQL markPullRequestReadyForReview mutation, "
        "which this proxy setup cannot reach. Use the GitHub web UI instead."
    )


# --------------------------------------------------------------------------
# issue
# --------------------------------------------------------------------------

def cmd_issue_create(args):
    owner, repo = resolve_repo(args.repo)
    title, body = args.title, read_body(args.body, args.body_file)
    if not title:
        raise GhError("--title is required")
    payload = {"title": title, "body": body or ""}
    if args.label:
        payload["labels"] = split_list(args.label)
    if args.assignee:
        payload["assignees"] = split_list(args.assignee)
    if args.milestone:
        payload["milestone"] = args.milestone
    if args.project:
        eprint("note: --project targets classic Projects only; Projects v2 has no REST endpoint and is ignored here")
    issue = request("POST", f"repos/{owner}/{repo}/issues", body=payload)
    print(issue["html_url"])


def cmd_issue_list(args):
    owner, repo = resolve_repo(args.repo)
    if args.search:
        q = [f"repo:{owner}/{repo}", "type:issue", f"state:{args.state}", args.search]
        result = request("GET", "search/issues", params={"q": " ".join(q), "per_page": args.limit})
        items = result.get("items", [])
    else:
        items = paged_list(
            f"repos/{owner}/{repo}/issues",
            params={"state": args.state, "labels": args.label or None, "assignee": args.assignee or None, "creator": args.author or None},
        )
        items = [i for i in items if "pull_request" not in i][: args.limit]
    for it in items:
        print(f"#{it['number']}\t{it['title']}\t{it['state']}")


def cmd_issue_view(args):
    owner, repo = resolve_repo(args.repo)
    issue = request("GET", f"repos/{owner}/{repo}/issues/{args.number}")
    if args.comments:
        comments = paged_list(f"repos/{owner}/{repo}/issues/{args.number}/comments")
        if args.json is not None:
            print(json.dumps(comments, indent=2))
        else:
            for c in comments:
                print(f"--- {c['user']['login']} ---\n{c['body']}\n")
        return
    if args.json is not None:
        emit_json_fields(issue, args.json)
        return
    print(f"#{issue['number']} {issue['title']} [{issue['state']}]")
    print(issue["html_url"])
    if issue.get("body"):
        print(f"\n{issue['body']}")
    if args.web:
        print("(--web: open the URL above manually; this host has no browser)")


def cmd_issue_comment(args):
    owner, repo = resolve_repo(args.repo)
    body = read_body(args.body, args.body_file)
    if not body:
        raise GhError("--body or --body-file is required")
    c = request("POST", f"repos/{owner}/{repo}/issues/{args.number}/comments", body={"body": body})
    print(c["html_url"])


def cmd_issue_edit(args):
    owner, repo = resolve_repo(args.repo)
    payload = {}
    if args.title:
        payload["title"] = args.title
    if args.body is not None or args.body_file:
        payload["body"] = read_body(args.body, args.body_file)
    if args.label:
        payload["labels"] = split_list(args.label)
    if args.assignee:
        payload["assignees"] = split_list(args.assignee)
    if args.milestone:
        payload["milestone"] = args.milestone
    if not payload:
        raise GhError("nothing to edit — pass at least one of --title/--body/--label/--assignee/--milestone")
    request("PATCH", f"repos/{owner}/{repo}/issues/{args.number}", body=payload)
    print(f"updated #{args.number}")


def cmd_issue_close(args):
    owner, repo = resolve_repo(args.repo)
    payload = {"state": "closed"}
    if args.reason:
        payload["state_reason"] = args.reason.replace(" ", "_")
    request("PATCH", f"repos/{owner}/{repo}/issues/{args.number}", body=payload)
    print(f"closed #{args.number}")


def cmd_issue_reopen(args):
    owner, repo = resolve_repo(args.repo)
    request("PATCH", f"repos/{owner}/{repo}/issues/{args.number}", body={"state": "open"})
    print(f"reopened #{args.number}")


# --------------------------------------------------------------------------
# workflow
# --------------------------------------------------------------------------

def cmd_workflow_list(args):
    owner, repo = resolve_repo(args.repo)
    workflows = request("GET", f"repos/{owner}/{repo}/actions/workflows", paginate=True)
    for wf in workflows:
        print(f"{wf['name']}\t{wf['state']}\t{wf['id']}")


def cmd_workflow_view(args):
    owner, repo = resolve_repo(args.repo)
    wf = request("GET", f"repos/{owner}/{repo}/actions/workflows/{args.workflow}")
    if args.yaml:
        content = request("GET", f"repos/{owner}/{repo}/contents/{wf['path']}")
        import base64 as _b64
        sys.stdout.write(_b64.b64decode(content["content"]).decode(errors="replace"))
        return
    print(json.dumps(wf, indent=2))


def cmd_workflow_run(args):
    owner, repo = resolve_repo(args.repo)
    inputs = {}
    for pair in args.field or []:
        k, _, v = pair.partition("=")
        inputs[k] = v
    ref = args.ref or current_branch()
    request(
        "POST", f"repos/{owner}/{repo}/actions/workflows/{args.workflow}/dispatches",
        body={"ref": ref, "inputs": inputs},
    )
    print(f"dispatched {args.workflow} on {ref}")


def cmd_workflow_enable(args):
    owner, repo = resolve_repo(args.repo)
    request("PUT", f"repos/{owner}/{repo}/actions/workflows/{args.workflow}/enable")
    print(f"enabled {args.workflow}")


def cmd_workflow_disable(args):
    owner, repo = resolve_repo(args.repo)
    request("PUT", f"repos/{owner}/{repo}/actions/workflows/{args.workflow}/disable")
    print(f"disabled {args.workflow}")


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def cmd_run_list(args):
    owner, repo = resolve_repo(args.repo)
    if args.workflow:
        path = f"repos/{owner}/{repo}/actions/workflows/{args.workflow}/runs"
    else:
        path = f"repos/{owner}/{repo}/actions/runs"
    runs = paged_list(path, params={"branch": args.branch, "status": args.status})[: args.limit]
    for r in runs:
        print(f"{r['id']}\t{r['name']}\t{r['status']}\t{r.get('conclusion') or ''}\t{r['head_branch']}")


def cmd_run_view(args):
    owner, repo = resolve_repo(args.repo)
    run = request("GET", f"repos/{owner}/{repo}/actions/runs/{args.run_id}")
    if args.web:
        print(run["html_url"])
        return
    if args.job:
        job = request("GET", f"repos/{owner}/{repo}/actions/jobs/{args.job}")
        print(json.dumps(job, indent=2))
        return
    if args.log or args.log_failed:
        jobs = paged_list(f"repos/{owner}/{repo}/actions/runs/{args.run_id}/jobs")
        for j in jobs:
            if args.log_failed and j.get("conclusion") == "success":
                continue
            print(f"=== {j['name']} ===")
            try:
                text = request("GET", f"repos/{owner}/{repo}/actions/jobs/{j['id']}/logs", raw=True)
                sys.stdout.write(text.decode(errors="replace"))
            except GhError as e:
                eprint(f"  (could not fetch logs: {e})")
        return
    print(f"{run['name']} — {run.get('conclusion') or run['status']}")
    print(run["html_url"])


def cmd_run_watch(args):
    owner, repo = resolve_repo(args.repo)
    run_endpoint = f"repos/{owner}/{repo}/actions/runs/{args.run_id}"

    def mark(j):
        if j["status"] != "completed":
            return "*" if j["status"] == "in_progress" else "."
        if j.get("conclusion") == "success":
            return "✓"
        if j.get("conclusion") == "skipped":
            return "-"
        return "X"

    while True:
        run = request("GET", run_endpoint)
        jobs = paged_list(f"{run_endpoint}/jobs")

        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
        print(f"{run.get('name', 'workflow')} — {run.get('conclusion') or run['status']}")
        print(run["html_url"])
        print()
        for j in jobs:
            print(f"{mark(j)} {j['name']} [{j.get('conclusion') or j['status']}]")
            for step in j.get("steps") or []:
                print(f"  {mark(step)} {step['name']}")
        print()

        if run["status"] == "completed":
            if args.exit_status and run.get("conclusion") != "success":
                raise SystemExit(1)
            return
        time.sleep(args.interval)


def cmd_run_rerun(args):
    owner, repo = resolve_repo(args.repo)
    suffix = "/rerun-failed-jobs" if args.failed else "/rerun"
    request("POST", f"repos/{owner}/{repo}/actions/runs/{args.run_id}{suffix}")
    print(f"re-running {args.run_id}")


def cmd_run_cancel(args):
    owner, repo = resolve_repo(args.repo)
    request("POST", f"repos/{owner}/{repo}/actions/runs/{args.run_id}/cancel")
    print(f"cancelled {args.run_id}")


# --------------------------------------------------------------------------
# api / auth
# --------------------------------------------------------------------------

def cmd_api(args):
    fields = {}
    for pair in args.raw_field or []:
        k, _, v = pair.partition("=")
        fields[k] = v
    for pair in args.typed_field or []:
        k, _, v = pair.partition("=")
        if v.startswith("@"):
            with open(v[1:]) as fh:
                v = fh.read()
        elif v.lower() in ("true", "false"):
            v = v.lower() == "true"
        else:
            try:
                v = int(v)
            except ValueError:
                try:
                    v = float(v)
                except ValueError:
                    pass
        fields[k] = v

    method = args.method or ("POST" if fields else "GET")
    headers = {}
    for h in args.header or []:
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()

    body = fields or None
    result = request(method, args.endpoint, body=body, headers=headers, paginate=args.paginate)
    if args.silent:
        return
    emit(result, args.jq)


def cmd_auth_status(args):
    try:
        user = request("GET", "user")
    except GhError as e:
        print(f"gh-rest.py: not authenticated ({e})")
        raise SystemExit(1)
    print(f"Logged in as {user.get('login', '?')}")


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gh", description="REST-only gh CLI replacement (gh-rest.py)")
    sub = p.add_subparsers(dest="command", required=True)

    # pr
    pr = sub.add_parser("pr").add_subparsers(dest="pr_cmd", required=True)

    c = pr.add_parser("create"); add_repo_flag(c)
    c.add_argument("--title"); c.add_argument("--body"); c.add_argument("--body-file")
    c.add_argument("--base"); c.add_argument("--head")
    c.add_argument("--draft", action="store_true"); c.add_argument("--fill", action="store_true")
    c.add_argument("--reviewer"); c.add_argument("--assignee"); c.add_argument("--label")
    c.add_argument("--web", action="store_true")
    c.set_defaults(func=cmd_pr_create)

    c = pr.add_parser("list"); add_repo_flag(c)
    c.add_argument("--state", default="open")
    c.add_argument("--author"); c.add_argument("--label"); c.add_argument("--base")
    c.add_argument("--limit", type=int, default=30)
    c.set_defaults(func=cmd_pr_list)

    c = pr.add_parser("view"); add_repo_flag(c)
    c.add_argument("number"); c.add_argument("--web", action="store_true")
    c.add_argument("--comments", action="store_true"); c.add_argument("--json")
    c.set_defaults(func=cmd_pr_view)

    c = pr.add_parser("diff"); add_repo_flag(c); c.add_argument("number")
    c.set_defaults(func=cmd_pr_diff)

    c = pr.add_parser("checkout"); c.add_argument("number")
    c.set_defaults(func=cmd_pr_checkout)

    c = pr.add_parser("merge"); add_repo_flag(c); c.add_argument("number")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--squash", action="store_true"); g.add_argument("--merge", action="store_true"); g.add_argument("--rebase", action="store_true")
    c.add_argument("--delete-branch", action="store_true"); c.add_argument("--auto", action="store_true")
    c.set_defaults(func=cmd_pr_merge)

    c = pr.add_parser("review"); add_repo_flag(c); c.add_argument("number")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true"); g.add_argument("--request-changes", action="store_true"); g.add_argument("--comment", action="store_true")
    c.add_argument("--body")
    c.set_defaults(func=cmd_pr_review)

    c = pr.add_parser("comment"); add_repo_flag(c); c.add_argument("number")
    c.add_argument("--body"); c.add_argument("--body-file")
    c.set_defaults(func=cmd_pr_comment)

    c = pr.add_parser("edit"); add_repo_flag(c); c.add_argument("number")
    c.add_argument("--title"); c.add_argument("--body"); c.add_argument("--body-file"); c.add_argument("--base")
    c.add_argument("--add-label"); c.add_argument("--remove-label")
    c.add_argument("--add-assignee"); c.add_argument("--remove-assignee")
    c.add_argument("--add-reviewer")
    c.set_defaults(func=cmd_pr_edit)

    c = pr.add_parser("close"); add_repo_flag(c); c.add_argument("number")
    c.set_defaults(func=cmd_pr_close)
    c = pr.add_parser("reopen"); add_repo_flag(c); c.add_argument("number")
    c.set_defaults(func=cmd_pr_reopen)
    c = pr.add_parser("ready"); add_repo_flag(c); c.add_argument("number")
    c.set_defaults(func=cmd_pr_ready)

    # issue
    issue = sub.add_parser("issue").add_subparsers(dest="issue_cmd", required=True)

    c = issue.add_parser("create"); add_repo_flag(c)
    c.add_argument("--title"); c.add_argument("--body"); c.add_argument("--body-file")
    c.add_argument("--label"); c.add_argument("--assignee"); c.add_argument("--milestone"); c.add_argument("--project")
    c.set_defaults(func=cmd_issue_create)

    c = issue.add_parser("list"); add_repo_flag(c)
    c.add_argument("--state", default="open")
    c.add_argument("--label"); c.add_argument("--assignee"); c.add_argument("--author"); c.add_argument("--search")
    c.add_argument("--limit", type=int, default=30)
    c.set_defaults(func=cmd_issue_list)

    c = issue.add_parser("view"); add_repo_flag(c)
    c.add_argument("number"); c.add_argument("--web", action="store_true")
    c.add_argument("--comments", action="store_true"); c.add_argument("--json")
    c.set_defaults(func=cmd_issue_view)

    c = issue.add_parser("comment"); add_repo_flag(c); c.add_argument("number")
    c.add_argument("--body"); c.add_argument("--body-file")
    c.set_defaults(func=cmd_issue_comment)

    c = issue.add_parser("edit"); add_repo_flag(c); c.add_argument("number")
    c.add_argument("--title"); c.add_argument("--body"); c.add_argument("--body-file")
    c.add_argument("--label"); c.add_argument("--assignee"); c.add_argument("--milestone")
    c.set_defaults(func=cmd_issue_edit)

    c = issue.add_parser("close"); add_repo_flag(c); c.add_argument("number"); c.add_argument("--reason")
    c.set_defaults(func=cmd_issue_close)
    c = issue.add_parser("reopen"); add_repo_flag(c); c.add_argument("number")
    c.set_defaults(func=cmd_issue_reopen)

    # workflow
    workflow = sub.add_parser("workflow").add_subparsers(dest="workflow_cmd", required=True)

    c = workflow.add_parser("list"); add_repo_flag(c)
    c.set_defaults(func=cmd_workflow_list)
    c = workflow.add_parser("view"); add_repo_flag(c); c.add_argument("workflow"); c.add_argument("--yaml", action="store_true")
    c.set_defaults(func=cmd_workflow_view)
    c = workflow.add_parser("run"); add_repo_flag(c); c.add_argument("workflow")
    c.add_argument("--ref"); c.add_argument("-f", "--field", dest="field", action="append")
    c.set_defaults(func=cmd_workflow_run)
    c = workflow.add_parser("enable"); add_repo_flag(c); c.add_argument("workflow")
    c.set_defaults(func=cmd_workflow_enable)
    c = workflow.add_parser("disable"); add_repo_flag(c); c.add_argument("workflow")
    c.set_defaults(func=cmd_workflow_disable)

    # run
    run = sub.add_parser("run").add_subparsers(dest="run_cmd", required=True)

    c = run.add_parser("list"); add_repo_flag(c)
    c.add_argument("--workflow"); c.add_argument("--branch"); c.add_argument("--status")
    c.add_argument("--limit", type=int, default=30)
    c.set_defaults(func=cmd_run_list)

    c = run.add_parser("view"); add_repo_flag(c); c.add_argument("run_id")
    c.add_argument("--log", action="store_true"); c.add_argument("--log-failed", action="store_true")
    c.add_argument("--job"); c.add_argument("--web", action="store_true")
    c.set_defaults(func=cmd_run_view)

    c = run.add_parser("watch"); add_repo_flag(c); c.add_argument("run_id")
    c.add_argument("--exit-status", action="store_true")
    c.add_argument("-i", "--interval", type=int, default=3)
    c.set_defaults(func=cmd_run_watch)

    c = run.add_parser("rerun"); add_repo_flag(c); c.add_argument("run_id"); c.add_argument("--failed", action="store_true")
    c.set_defaults(func=cmd_run_rerun)
    c = run.add_parser("cancel"); add_repo_flag(c); c.add_argument("run_id")
    c.set_defaults(func=cmd_run_cancel)

    # api (generic passthrough — same shape as real `gh api`)
    c = sub.add_parser("api")
    c.add_argument("endpoint")
    c.add_argument("-X", "--method")
    c.add_argument("-f", dest="raw_field", action="append")
    c.add_argument("-F", dest="typed_field", action="append")
    c.add_argument("-H", "--header", action="append")
    c.add_argument("--jq")
    c.add_argument("--paginate", action="store_true")
    c.add_argument("--silent", action="store_true")
    c.set_defaults(func=cmd_api)

    # auth
    auth = sub.add_parser("auth").add_subparsers(dest="auth_cmd", required=True)
    c = auth.add_parser("status")
    c.set_defaults(func=cmd_auth_status)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except GhError as e:
        eprint(f"gh-rest.py: {e}")
        raise SystemExit(1)
    except subprocess.CalledProcessError as e:
        raise SystemExit(e.returncode)


if __name__ == "__main__":
    main()
