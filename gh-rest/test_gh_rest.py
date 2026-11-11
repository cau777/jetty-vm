import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
from pathlib import Path
import unittest
import zipfile
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("gh_rest", Path(__file__).with_name("gh-rest.py"))
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)

FIELDS = "number,url,baseRefName,headRefName,state,isDraft"
PR = {
    "number": 42, "html_url": "https://github.com/acme/app/pull/42",
    "title": "Billing receipt", "state": "open", "draft": True,
    "head": {"ref": "feat/billing-receipt"}, "base": {"ref": "main"},
}


class PrListTests(unittest.TestCase):
    def run_list(self, *flags, items=None):
        output = io.StringIO()
        with patch.object(gh, "request", return_value=[PR] if items is None else items) as request:
            with contextlib.redirect_stdout(output):
                gh.main(["pr", "list", "-R", "acme/app", *flags])
        return output.getvalue(), request

    def test_reported_command_filters_and_maps_json_fields(self):
        other = dict(PR, number=43, head={"ref": "feat/billing-receipt-extra"})
        output, request = self.run_list("--head", "feat/billing-receipt", "--json", FIELDS,
                                        "--limit", "1", items=[other, PR])
        self.assertEqual(json.loads(output), [{
            "number": 42, "url": PR["html_url"], "baseRefName": "main",
            "headRefName": "feat/billing-receipt", "state": "OPEN", "isDraft": True,
        }])
        keep = request.call_args.kwargs["keep"]
        self.assertFalse(keep(other))
        self.assertTrue(keep(PR))

    def test_empty_json_result(self):
        output, _ = self.run_list("--head", "missing", "--json", FIELDS, items=[PR])
        self.assertEqual(json.loads(output), [])

    def test_closed_and_merged_json_states(self):
        for merged_at, state in [(None, "CLOSED"), ("2026-10-28T00:00:00Z", "MERGED")]:
            with self.subTest(state=state):
                output, _ = self.run_list("--json", "state,isDraft", items=[
                    dict(PR, state="closed", draft=False, merged_at=merged_at)])
                self.assertEqual(json.loads(output), [{"state": state, "isDraft": False}])

    def test_plain_output_still_works(self):
        output, _ = self.run_list("--head", "feat/billing-receipt")
        self.assertEqual(output, "#42\tBilling receipt\tfeat/billing-receipt\topen\n")

    def test_search_json_fetches_pull_request_details(self):
        output = io.StringIO()
        with patch.object(gh, "request", side_effect=[{"items": [{"number": 42}]}, PR]) as request:
            with contextlib.redirect_stdout(output):
                gh.main(["pr", "list", "-R", "acme/app", "--author", "alice",
                         "--head", "feat/billing-receipt", "--json", FIELDS])
        self.assertEqual(json.loads(output.getvalue())[0]["headRefName"], "feat/billing-receipt")
        self.assertIn("head:feat/billing-receipt", request.call_args_list[0].kwargs["params"]["q"])
        self.assertEqual(request.call_args_list[1].args, ("GET", "repos/acme/app/pulls/42"))


class PrViewTests(unittest.TestCase):
    def view(self, *flags):
        output = io.StringIO()
        with patch.object(gh, "request", return_value=dict(PR, body="First line\n\nSecond line")) as request:
            with contextlib.redirect_stdout(output):
                gh.main(["pr", "view", "285", "-R", "acme/app", *flags])
        request.assert_called_once_with("GET", "repos/acme/app/pulls/285")
        return output.getvalue()

    def test_reported_command_prints_raw_body(self):
        self.assertEqual(self.view("--json", "body", "-q", ".body"),
                         "First line\n\nSecond line\n")

    def test_long_jq_flag_filters_selected_fields(self):
        self.assertEqual(self.view("--json", "body", "--jq", "keys | join(\",\")"), "body\n")

    def test_json_without_jq_still_works(self):
        self.assertEqual(json.loads(self.view("--json", "body")),
                         {"body": "First line\n\nSecond line"})


class RunListTests(unittest.TestCase):
    RUN = {
        "id": 123, "name": "CI", "status": "completed",
        "conclusion": "success", "head_branch": "feat/nuxt-example",
    }

    def run_list(self, *flags, items=None):
        output = io.StringIO()
        with patch.object(gh, "request", return_value=[self.RUN] if items is None else items) as request:
            with contextlib.redirect_stdout(output):
                gh.main(["run", "list", "-R", "acme/app", *flags])
        return output.getvalue(), request

    def test_reported_command_maps_and_selects_json_fields(self):
        output, request = self.run_list(
            "--branch", "feat/nuxt-example", "--limit", "4",
            "--json", "databaseId,status,conclusion,name", items=[self.RUN] * 5)
        self.assertEqual(json.loads(output), [{
            "databaseId": 123, "status": "completed", "conclusion": "success", "name": "CI",
        }] * 4)
        self.assertEqual(request.call_args.args, ("GET", "repos/acme/app/actions/runs"))
        self.assertEqual(request.call_args.kwargs["params"]["branch"], "feat/nuxt-example")
        self.assertEqual(request.call_args.kwargs["limit"], 4)

    def test_empty_json_result(self):
        output, _ = self.run_list("--json", "databaseId", items=[])
        self.assertEqual(json.loads(output), [])

    def test_in_progress_run_has_null_conclusion(self):
        output, _ = self.run_list("--json", "status,conclusion", items=[
            dict(self.RUN, status="in_progress", conclusion=None)])
        self.assertEqual(json.loads(output), [{"status": "in_progress", "conclusion": None}])

    def test_plain_output_still_works(self):
        output, _ = self.run_list()
        self.assertEqual(output, "123\tCI\tcompleted\tsuccess\tfeat/nuxt-example\n")


class AuthTokenTests(unittest.TestCase):
    def test_prints_fake_token_without_network(self):
        output = io.StringIO()
        with patch.object(gh, "request") as request:
            with contextlib.redirect_stdout(output):
                gh.main(["auth", "token", "--hostname", "github.com"])
        self.assertEqual(output.getvalue(), "gho_FAKE_TOKEN_INJECTED_BY_PROXY\n")
        request.assert_not_called()


class HostnameTests(unittest.TestCase):
    def run_api(self, *flags):
        with patch.object(gh, "request", return_value={}) as request:
            with contextlib.redirect_stdout(io.StringIO()):
                gh.main(["api", *flags])
        return request.call_args.args[1]

    def test_api_without_hostname_keeps_relative_endpoint(self):
        self.assertEqual(self.run_api("repos/acme/app"), "repos/acme/app")

    def test_api_github_com_hostname(self):
        self.assertEqual(self.run_api("--hostname", "github.com", "/repos/acme/app"),
                         "https://api.github.com/repos/acme/app")

    def test_api_enterprise_hostname(self):
        self.assertEqual(self.run_api("--hostname", "ghe.acme.com", "repos/acme/app"),
                         "https://ghe.acme.com/api/v3/repos/acme/app")

    def test_api_hostname_leaves_absolute_url_alone(self):
        url = "https://api.github.com/repos/acme/app"
        self.assertEqual(self.run_api("--hostname", "ghe.acme.com", url), url)

    def test_auth_status_accepts_hostname(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            gh.main(["auth", "status", "--hostname", "github.com"])
        self.assertIn("authenticated", output.getvalue())


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


class RunDownloadTests(unittest.TestCase):
    ARTIFACTS = [
        {"name": "dist", "expired": False, "archive_download_url": "https://api.github.com/a/1/zip"},
        {"name": "coverage", "expired": False, "archive_download_url": "https://api.github.com/a/2/zip"},
        {"name": "old", "expired": True, "archive_download_url": "https://api.github.com/a/3/zip"},
    ]
    ZIPS = {
        "https://api.github.com/a/1/zip": zip_bytes({"app.js": "js", "lib/util.js": "util"}),
        "https://api.github.com/a/2/zip": zip_bytes({"index.html": "html"}),
    }

    def fake_request(self, method, path, out=None, **kw):
        if out is None:
            return self.ARTIFACTS
        out.write(self.ZIPS[path])

    def download(self, *flags):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with patch.object(gh, "request", side_effect=self.fake_request) as request:
            gh.main(["run", "download", "-R", "acme/app", "-D", tmp, *flags])
        files = sorted(str(p.relative_to(tmp)) for p in Path(tmp).rglob("*") if p.is_file())
        return tmp, files, request

    def test_all_artifacts_isolated_and_expired_skipped(self):
        _, files, request = self.download("123")
        self.assertEqual(files, ["coverage/index.html", "dist/app.js", "dist/lib/util.js"])
        self.assertEqual(request.call_args_list[0].args, ("GET", "repos/acme/app/actions/runs/123/artifacts"))

    def test_single_name_extracts_into_dir(self):
        _, files, _ = self.download("123", "-n", "dist")
        self.assertEqual(files, ["app.js", "lib/util.js"])

    def test_pattern_isolates(self):
        _, files, _ = self.download("123", "-p", "cov*")
        self.assertEqual(files, ["coverage/index.html"])

    def test_no_run_id_uses_repo_artifacts(self):
        _, _, request = self.download("-n", "dist")
        self.assertEqual(request.call_args_list[0].args, ("GET", "repos/acme/app/actions/artifacts"))

    def test_no_match_fails(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.download("123", "-n", "old")

    def test_refuses_overwrite_and_zip_slip(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        Path(tmp, "a.txt").write_text("keep")
        with self.assertRaisesRegex(gh.GhError, "already exists"):
            gh.extract_artifact(io.BytesIO(zip_bytes({"a.txt": "new"})), tmp)
        self.assertEqual(Path(tmp, "a.txt").read_text(), "keep")
        with self.assertRaisesRegex(gh.GhError, "outside"):
            gh.extract_artifact(io.BytesIO(zip_bytes({"../evil.txt": "x"})), tmp)


if __name__ == "__main__":
    unittest.main()


class BooleanOutputTests(unittest.TestCase):
    def test_boolean_scalars_use_json_spelling(self):
        for value, expected in [(True, 'true\n'), (False, 'false\n')]:
            with self.subTest(value=value), contextlib.redirect_stdout(io.StringIO()) as output:
                gh.emit(value, None)
                self.assertEqual(output.getvalue(), expected)
