import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
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


if __name__ == "__main__":
    unittest.main()
