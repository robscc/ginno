"""API tests for POST /skills/import-dir (batch import from a directory)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.api


def _skill_dir(root: Path, name: str, md_name: str, desc: str, extra: dict[str, str] | None = None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / md_name).write_text(
        f"---\nname: {name}\ndescription: {desc}\ntrigger: both\n---\n\n# {name}\n\nbody\n",
        encoding="utf-8",
    )
    for fn, content in (extra or {}).items():
        (d / fn).write_text(content, encoding="utf-8")
    return d


def test_import_dir_copies_skills_and_extras(isolated_home, client, tmp_path):
    src = tmp_path / "src_skills"
    _skill_dir(src, "alpha", "SKILL.md", "alpha desc")
    _skill_dir(src, "beta", "skill.md", "beta desc (lowercase md)")  # lowercase skill.md
    _skill_dir(src, "gamma", "SKILL.md", "gamma with script", extra={"run.py": "print(1)\n", "ref.md": "ref"})
    (src / "not-a-skill").mkdir()  # no SKILL.md → skipped from scan

    r = client.post("/skills/import-dir", json={"path": str(src)}).json()
    assert r["ok"] is True
    names = {x["name"] for x in r["imported"]}
    assert names == {"alpha", "beta", "gamma"}
    assert r["scanned"] == 3  # not-a-skill excluded (no SKILL.md)

    gskills = isolated_home / "skills"
    # lowercase skill.md is loadable: renamed to SKILL.md on case-sensitive FS,
    # matched directly on case-insensitive FS — both verified via the list endpoint
    assert (gskills / "beta").is_dir()
    # extra files copied
    assert (gskills / "gamma" / "run.py").read_text(encoding="utf-8") == "print(1)\n"
    assert (gskills / "gamma" / "ref.md").exists()

    # and they show up in the list endpoint
    listed = {s["name"] for s in client.get("/skills").json()}
    assert {"alpha", "beta", "gamma"} <= listed


def test_import_dir_skip_existing_then_overwrite(isolated_home, client, tmp_path):
    src = tmp_path / "src_skills"
    _skill_dir(src, "alpha", "SKILL.md", "v1")

    r1 = client.post("/skills/import-dir", json={"path": str(src)}).json()
    assert {x["name"] for x in r1["imported"]} == {"alpha"}

    # change source, import again without overwrite → skipped
    (src / "alpha" / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: v2\n---\n# alpha\n\nnew body\n", encoding="utf-8"
    )
    r2 = client.post("/skills/import-dir", json={"path": str(src)}).json()
    assert r2["skipped"] and r2["skipped"][0]["name"] == "alpha"
    assert "new body" not in (isolated_home / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8")

    # with overwrite → updated
    r3 = client.post("/skills/import-dir", json={"path": str(src), "overwrite": True}).json()
    assert {x["name"] for x in r3["imported"]} == {"alpha"}
    assert "new body" in (isolated_home / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8")


def test_import_dir_invalid_path(client):
    assert client.post("/skills/import-dir", json={"path": ""}).json()["ok"] is False
    assert client.post("/skills/import-dir", json={"path": "/no/such/dir/xyz"}).json()["ok"] is False


def test_import_dir_single_skill_dir(isolated_home, client, tmp_path):
    # path points directly at one skill dir
    _skill_dir(tmp_path, "solo", "SKILL.md", "solo desc")
    r = client.post("/skills/import-dir", json={"path": str(tmp_path / "solo")}).json()
    assert {x["name"] for x in r["imported"]} == {"solo"}
    assert (isolated_home / "skills" / "solo" / "SKILL.md").exists()
