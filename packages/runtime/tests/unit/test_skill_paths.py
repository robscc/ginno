"""Session skill discovery, permission boundaries and persistence contract."""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from ginno_runtime import paths, server_shared
from ginno_runtime.api.sessions import CreateSessionRequest, _ensure_session, _validate_skill_paths
from ginno_runtime.commands.resolver import resolve_turn
from ginno_runtime.skills.loader import SkillLoader, session_access_roots
from ginno_runtime.tools.skill_tools import build_skill_tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def nested_home(isolated_home, monkeypatch):
    home = isolated_home / "runtime-home"
    home.mkdir()
    monkeypatch.setenv("GINNO_HOME", str(home))



def skill(root, name="local-demo", body="PRIVATE BODY", bindings=""):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: summary\n{bindings}---\n{body}"
    )
    return d


def tool(dirs=(), roots=()):
    return next(t for t in build_skill_tools(skill_dirs=list(dirs), skill_access_roots=roots)
                if t.name == "use_skill")


def test_schema():
    assert CreateSessionRequest(project_slug="default", workspace="/tmp").skill_paths == []
    for invalid in [None, "abc", [1], {}]:
        with pytest.raises(ValidationError):
            CreateSessionRequest(project_slug="default", workspace="/tmp", skill_paths=invalid)


def test_validation(tmp_path):
    assert _validate_skill_paths([str(tmp_path), str(tmp_path)]) == [str(tmp_path.resolve())]
    for bad in ["relative", "", str(tmp_path / "missing")]:
        with pytest.raises(HTTPException) as exc:
            _validate_skill_paths([bad])
        assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        _validate_skill_paths([str(tmp_path)] * 21)


def test_protected_and_symlink(tmp_path, monkeypatch):
    from ginno_runtime.tools import builtin
    secret = tmp_path / "secret"
    secret.mkdir()
    skill(secret)
    monkeypatch.setattr(builtin, "_always_deny_roots", lambda: [str(secret)])
    builtin._POLICY_CACHE.clear()
    link = tmp_path / "link"
    link.symlink_to(secret, target_is_directory=True)
    for path in [secret, link]:
        with pytest.raises(HTTPException):
            _validate_skill_paths([str(path)], {"context_dirs": [{"path": str(tmp_path)}]})
        assert SkillLoader(extra_dirs=(str(path),)).get("local-demo") is None


def test_symlink_skill_escape_and_retarget(tmp_path):
    root = tmp_path / "package"
    d = skill(root)
    loader = SkillLoader(extra_dirs=(str(root),))
    assert loader.get("local-demo")
    outside = tmp_path / "outside.md"
    outside.write_text((d / "SKILL.md").read_text())
    (d / "SKILL.md").unlink()
    (d / "SKILL.md").symlink_to(outside)
    assert loader.get("local-demo") is None


def test_mount_permission_is_session_local(isolated_home):
    d = skill(paths.home() / "custom")
    with pytest.raises(HTTPException):
        _validate_skill_paths([str(d)])
    ctx = {"context_dirs": [{"path": str(d.parent), "access": "ro"}]}
    roots = session_access_roots(ctx)
    assert _validate_skill_paths([str(d)], ctx) == [str(d)]
    assert "PRIVATE BODY" in tool([str(d)], roots).invoke({"name": "local-demo"})
    assert SkillLoader(extra_dirs=(str(d),)).get("local-demo") is None


def test_original_directory_index_and_isolation(tmp_path):
    d = skill(tmp_path)
    (d / "script.py").write_text("print('ok')")
    loader = SkillLoader(extra_dirs=(str(d),))
    assert "summary" in loader.build_index_prompt()
    assert "PRIVATE BODY" not in loader.build_index_prompt()
    result = tool([str(d)]).invoke({"name": "local-demo"})
    assert "PRIVATE BODY" in result and str(d.resolve()) in result
    assert (loader.get("local-demo").path.parent / "script.py").read_text() == "print('ok')"
    assert tool().invoke({"name": "local-demo"}).startswith("[error]")
    assert not (paths.global_skills_dir() / "local-demo").exists()


def test_precedence_and_builtin_binding(tmp_path, monkeypatch):
    builtin = tmp_path / "builtin"
    skill(builtin, bindings="tools: [todo_list]\nsticky: true\n")
    monkeypatch.setattr(paths, "builtin_skills_dir", lambda: builtin)
    skill(paths.global_skills_dir(), body="GLOBAL")
    skill(paths.project_skills_dir("default"), body="PROJECT")
    one = skill(tmp_path / "one", body="ONE")
    two = skill(tmp_path / "two", body="TWO")
    loader = SkillLoader("default", (str(one), str(two)))
    selected = loader.get("local-demo")
    assert selected.body == "TWO"
    assert selected.allowed_tools == ["todo_list"] and selected.sticky
    assert SkillLoader("default").get("local-demo").body == "PROJECT"
    assert SkillLoader().get("local-demo").body == "GLOBAL"


def test_raw_user_message_not_expanded(tmp_path):
    d = skill(tmp_path)
    for text in ["please use local-demo", "/local-demo do this"]:
        plan = resolve_turn({"message": text}, {"project_slug": "default", "skill_dirs": [str(d)]})
        assert plan.text == text and "PRIVATE BODY" not in plan.text


def test_static_slash_still_substitutes_in_skill_dirs_session(tmp_path, monkeypatch):
    """skill_dirs 会话中，静态层技能的斜杠展开不被静默禁用（review M1）。"""
    builtin = tmp_path / "builtin"
    skill(builtin, name="slashdemo", body="# Builtin demo")
    monkeypatch.setattr(paths, "builtin_skills_dir", lambda: builtin)
    d = skill(tmp_path / "sess")
    plan = resolve_turn({"message": "/slashdemo tail args"},
                        {"project_slug": "default", "skill_dirs": [str(d)]})
    assert plan.text.startswith('<skill name="slashdemo">') and "# Builtin demo" in plan.text
    assert "tail args" in plan.text and plan.skill_name == "slashdemo"
    # 会话级技能仍不做斜杠展开，原文保留。
    raw = resolve_turn({"message": "/local-demo x"},
                       {"project_slug": "default", "skill_dirs": [str(d)]})
    assert raw.text == "/local-demo x" and "PRIVATE BODY" not in raw.text


def test_create_restore_and_two_sessions(client, patch_build_model, tmp_path):
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script
    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    d = skill(tmp_path)
    first = client.post("/api/sessions", json={"project_slug": "default", "workspace": "/tmp", "skill_paths": [str(d)], "agent_id": "dev"})
    assert first.status_code == 200, first.text
    meta = first.json()
    assert meta["skill_dirs"] == [str(d.resolve())]
    second = client.post("/api/sessions", json={"project_slug": "default", "workspace": "/tmp", "agent_id": "dev"}).json()
    assert "skill_dirs" not in second
    server_shared._SESSIONS.pop(meta["id"])
    restored = _ensure_session(meta["id"])
    assert restored["skill_dirs"] == meta["skill_dirs"]
    assert _ensure_session(second["id"])["skill_dirs"] == []
    assert client.post("/api/sessions", json={"project_slug": "default", "workspace": "/tmp", "skill_paths": ["relative"]}).status_code == 422


def test_actual_stable_system_uses_session_index(tmp_path):
    from ginno_runtime.graph_prompts import build_stable_system
    d = skill(tmp_path)
    prompt = build_stable_system(None, "default", [], skill_dirs=[str(d)])
    assert "local-demo: summary" in prompt
    assert "PRIVATE BODY" not in prompt
    assert "local-demo: summary" not in build_stable_system(None, "default", [])


def test_credential_file_link_denied_even_inside_mount(tmp_path):
    d = skill(tmp_path)
    (d / "SKILL.md").unlink()
    (d / "SKILL.md").symlink_to(paths.settings_path())
    paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path().write_text("SECRET")
    assert SkillLoader(extra_dirs=(str(d),), access_roots=(str(tmp_path),)).get("local-demo") is None


def test_ws_keeps_user_text_and_skill_state(client, patch_build_model, tmp_path, ws_conv):
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script
    patch_build_model(ScriptedChatModel(scripts=[script(text="done")]))
    d = skill(tmp_path)
    meta = client.post("/api/sessions", json={
        "project_slug": "default", "workspace": "/tmp", "agent_id": "dev",
        "skill_paths": [str(d)],
    }).json()
    with ws_conv(meta["id"]) as conv:
        conv.invoke("/local-demo original request")
        events = conv.recv_until("message.end", "error")
    assert not [e for e in events if e.get("event") == "error"]
    session = _ensure_session(meta["id"])
    state = session["graph"].get_state({"configurable": {"thread_id": meta["id"]}}).values
    assert state["skill_dirs"] == [str(d)]
    human = [m for m in state["messages"] if m.type == "human"]
    assert any(m.content == "/local-demo original request" for m in human)
    assert all("PRIVATE BODY" not in str(m.content) for m in human)


def test_index_metadata_only_and_directory_link_escape(tmp_path):
    outside = skill(tmp_path / "outside")
    root = tmp_path / "collection"
    root.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    assert SkillLoader(extra_dirs=(str(root),)).get("local-demo") is None
    d = skill(root)
    metadata = SkillLoader(extra_dirs=(str(d),)).load(include_body=False)
    assert next(s for s in metadata if s.name == "local-demo").body == ""


def test_allowed_root_symlink_is_canonicalized(tmp_path):
    d = skill(tmp_path)
    link = tmp_path / "alias"
    link.symlink_to(d, target_is_directory=True)
    assert _validate_skill_paths([str(link), str(d)]) == [str(d.resolve())]
