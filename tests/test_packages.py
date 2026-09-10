from __future__ import annotations

import filecmp
from pathlib import Path

from scripts.verify_homeassistant_package import REQUIRED_HELPERS, parse_package

ROOT = Path(__file__).resolve().parents[1]


def test_helper_package_contains_phase_2_entities() -> None:
    text = (ROOT / "homeassistant" / "packages" / "energy_v2_helpers.yaml").read_text(encoding="utf-8")
    sections, _initials, errors = parse_package(text)

    assert not errors
    for section, helpers in REQUIRED_HELPERS.items():
        assert helpers <= sections.get(section, set())


def test_deployment_files_match_source_apps() -> None:
    source = ROOT / "apps"
    deploy = ROOT / "deploy" / "appdaemon" / "apps"
    source_files = {
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    deploy_files = {
        path.relative_to(deploy)
        for path in deploy.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }

    assert source_files == deploy_files
    assert all(filecmp.cmp(source / path, deploy / path, shallow=False) for path in source_files)
