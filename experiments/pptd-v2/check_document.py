"""Verify document authority and unsupported-field retention in this disposable spike.

This checks invariants, not the full PPTD schema or visual equivalence.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import tempfile
from pathlib import Path

import yaml
from server import Workspace, Yaml12SafeLoader, load_yaml


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def run(source):
    checks = []
    before = hashes(source.parent)
    with tempfile.TemporaryDirectory(prefix="pptd-probe-") as temporary:
        working = Path(temporary) / "working"
        workspace = Workspace(source, working)
        original = workspace.read()
        expected = copy.deepcopy(original)
        # Edit the very page containing unimplemented animations, icon and math.
        target = expected["pages"][1]["data"]["elements"][0]
        target["content"]["text"] = "保留未实现字段，只修改指定文字"
        target["bounds"][0] = 88
        result = workspace.edit({"pageIndex": 1, "elementId": target["elementId"],
                                 "text": target["content"]["text"], "bounds": target["bounds"]})
        assert result["verified"] and workspace.read() == expected
        checks.append("same-page animation, icon, math and every other field retained after text/bounds edit")
        assert Workspace(source, working).read() == expected
        checks.append("fresh Workspace reopens authoritative .pptd/.page files with edited values")
        for reference in [source.name, original["pages"][0]["path"], "media/fixture.png"]:
            assert (working / reference).read_bytes() == (source.parent / reference).read_bytes()
        checks.append("unedited entry/page/media remain byte-identical")
        assert workspace.metadata_path.parent == working.parent
        assert not list(working.glob("*.json"))
        checks.append("App edit metadata remains outside the PPTD project; no JSON master document")
        persisted = hashes(working)
        for payload in [
            {"pageIndex": 0, "elementId": "title", "bounds": [0, 0, -1, 10]},
            {"pageIndex": 0, "elementId": "absent", "text": "lost"},
            {"page": "../escape.page", "elementId": "title", "text": "lost"},
            {"pageIndex": 0, "elementId": "title", "title": "unknown operation"},
        ]:
            try:
                workspace.edit(payload)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid edit was accepted")
            assert hashes(working) == persisted
        checks.append("invalid bounds, missing IDs, outside paths and unknown edit keys rejected without file changes")
        sample = "text: on\nzero: 012\nscientific: 1e3\ndate: 2026-10-09\n"
        patched = yaml.load(sample, Loader=Yaml12SafeLoader)
        assert patched == {"text": "on", "zero": 12, "scientific": 1000.0, "date": "2026-10-09"}
        checks.append("selected YAML 1.2 scalar cases pass (not full conformance)")
        try:
            yaml.load("text: first\ntext: second\n", Loader=Yaml12SafeLoader)
        except ValueError:
            pass
        else:
            raise AssertionError("Duplicate key silently accepted")
        checks.append("duplicate YAML keys rejected")
        alias_file = Path(temporary) / "alias.page"
        alias_file.write_text("elements:\n  - elementId: first\n    content: &shared {text: before}\n  - elementId: second\n    content: *shared\n", encoding="utf-8")
        try:
            load_yaml(alias_file)
        except ValueError as error:
            assert "aliases" in str(error)
        else:
            raise AssertionError("Shared alias could mutate an unselected element")
        checks.append("YAML aliases explicitly rejected to prevent edits affecting an unselected element")
    assert hashes(source.parent) == before
    checks.append("original fixture remains byte-identical after all edit tests")
    return {"status": "passed", "scope": "spike document invariants, not full PPTD compatibility",
            "checks": checks, "fixtureHashes": before,
            "parserEvidence": {"sample": sample, "pyyamlDefault": yaml.safe_load(sample), "probeLoader": patched}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path(__file__).parent / "fixture" / "probe.pptd")
    parser.add_argument("--output", type=Path, default=Path("runtime/pptd-v2-spike/document-report.json"))
    args = parser.parse_args()
    result = run(args.input.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "checks": len(result["checks"]), "report": str(args.output)}, ensure_ascii=False))
