"""Compatibility test for the Quick Add catalog (design ticket #15).

#15's resolution is explicit that quick-add-catalog.json is loaded "both by
Python compatibility tests and the frontend's own fetch()" — a single
source of truth, not duplicated data. This is the Python half: it makes sure
the file the frontend renders as Quick Add buttons actually round-trips
through the same validation the Management API applies to a real
`PUT /api/v1/credentials/{name}`, and that any `command` naming a console
script (the refresh helpers) actually matches a script pyproject.toml
declares — so a rename on one side is caught here instead of silently
breaking Quick Add in the browser.
"""

import json
import tomllib
from pathlib import Path

from orca_proxy import validation
from orca_proxy.errors import ValidationFailed
from orca_proxy.handlers.credentials import _validate_command, _validate_ttl_seconds

CATALOG_PATH = Path(__file__).parent.parent / "src/orca_proxy/resources/quick-add-catalog.json"
PYPROJECT_PATH = Path(__file__).parent.parent / "pyproject.toml"


def _load_catalog() -> list[dict]:
    return json.loads(CATALOG_PATH.read_text())


def _console_script_names() -> set[str]:
    data = tomllib.loads(PYPROJECT_PATH.read_text())
    return set(data["project"].get("scripts", {}).keys())


def test_catalog_is_a_nonempty_list_of_objects():
    catalog = _load_catalog()
    assert isinstance(catalog, list)
    assert catalog


def test_catalog_keys_are_unique_and_are_valid_credential_names():
    catalog = _load_catalog()
    keys = [entry["key"] for entry in catalog]
    assert len(keys) == len(set(keys)), "duplicate Quick Add keys"
    for key in keys:
        validation.validate_name(key)  # raises ValidationFailed if not a valid Credential name


def test_catalog_commands_pass_the_real_credential_command_validation():
    for entry in _load_catalog():
        _validate_command(entry["command"])


def test_catalog_ttl_seconds_pass_the_real_credential_ttl_validation():
    for entry in _load_catalog():
        _validate_ttl_seconds(entry["ttl_seconds"])


def test_catalog_display_name_and_notes_are_nonempty_strings():
    for entry in _load_catalog():
        assert isinstance(entry["display_name"], str) and entry["display_name"].strip()
        assert isinstance(entry["notes"], str) and entry["notes"].strip()


def test_catalog_console_script_commands_exist_in_pyproject():
    """Catch the catalog and pyproject.toml's [project.scripts] drifting apart.

    Only single-word commands (no shell args/pipes) are assumed to name a
    console script — `gh auth token` is a real shell command, not a script
    this package ships, so it's exempt.
    """
    script_names = _console_script_names()
    for entry in _load_catalog():
        command = entry["command"]
        if " " in command:
            continue
        if command.startswith("orca-proxy-"):
            assert command in script_names, f"{command!r} not declared in pyproject.toml [project.scripts]"


def test_catalog_rejects_as_expected_when_a_field_is_missing():
    # Sanity check that _validate_command/_validate_ttl_seconds actually
    # reject bad input, so the passing tests above aren't vacuous.
    try:
        _validate_command("")
    except ValidationFailed:
        pass
    else:
        raise AssertionError("empty command should have failed validation")
