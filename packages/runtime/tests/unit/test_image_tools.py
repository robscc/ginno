"""Exercise the Responses connection's image tool against Magpie's wire shape."""

import base64
import json
from pathlib import Path

import httpx
import pytest

from ginno_runtime import providers, server_shared
from ginno_runtime.files import get_registry
from ginno_runtime.files.images import parse_images_marker
from ginno_runtime.files.registry import reset_registries
from ginno_runtime.models import build_model
from ginno_runtime.tools.image_tools import build_image_tools

pytestmark = pytest.mark.unit

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def isolate_file_registry():
    reset_registries()
    yield
    reset_registries()


@pytest.fixture
def responses_model(monkeypatch):
    monkeypatch.delenv("GINNO_FAKE_LLM", raising=False)

    def build(**fields):
        cfg = providers.normalize_config({
            "id": "magpie", "protocol": "openai-responses", "enabled": True,
            "default_model": "codex/chat", "api_key": "magpie-test",
            "base_url": "http://127.0.0.1:3425/v1", "timeout_s": 30,
            **fields,
        })
        monkeypatch.setattr(providers, "load_providers", lambda: {"magpie": cfg})
        model = build_model("magpie", session_id="image-session")
        monkeypatch.setitem(server_shared._SESSIONS, "image-session", {"model": model})
        return model

    return build


async def test_image_tool_uses_same_connection_and_saves_registry_entry(responses_model):
    model = responses_model(enable_image_generation=True)
    requests = []

    def endpoint(request):
        requests.append(request)
        return httpx.Response(200, json={
            "created": 1,
            "model": "codex/gpt-image-2",
            "data": [{"b64_json": base64.b64encode(PNG).decode(), "mime_type": "image/png"}],
        })

    generate, = build_image_tools("default", "image-session")
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as transport:
        model.root_async_client._client = transport
        result = await generate.ainvoke({"prompt": "A blue circle"})
    request, = requests
    assert str(request.url) == "http://127.0.0.1:3425/v1/images/generations"
    assert request.headers["Authorization"] == "Bearer magpie-test"
    assert request.headers["x-session-id"] == "image-session"
    assert json.loads(request.content) == {"model": "gpt-image-2", "prompt": "A blue circle", "n": 1}
    assert request.extensions["timeout"]["read"] == 300
    path = Path(parse_images_marker(result)[0])
    assert path.suffix == ".png" and path.read_bytes() == PNG
    file, = get_registry("default").list_session("image-session")
    assert file["path"] == str(path) and file["kind"] == "image"


async def test_disabled_tool_rejects_a_stale_call_before_network(responses_model):
    model = responses_model()
    assert model.image_generation_model == ""
    generate, = build_image_tools("default", "image-session")

    def no_request(_):
        pytest.fail("image request made with the switch off")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_request)) as transport:
        model.root_async_client._client = transport
        with pytest.raises(ValueError, match="开启生图|Enable image generation"):
            await generate.ainvoke({"prompt": "A blue circle"})


@pytest.mark.parametrize("invalid", [{"url": "https://example.test/image.png"}, {"b64_json": "@@invalid"}])
async def test_bad_image_payload_creates_no_file(responses_model, invalid):
    model = responses_model(enable_image_generation=True)
    generate, = build_image_tools("default", "image-session")
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"created": 1, "data": [invalid]}))
    async with httpx.AsyncClient(transport=transport) as client:
        model.root_async_client._client = client
        with pytest.raises(ValueError, match="Base64 图片|base64 image"):
            await generate.ainvoke({"prompt": "A blue circle"})
    assert get_registry("default").list_session("image-session") == []


async def test_server_failure_sends_only_one_generation_request(responses_model):
    import openai

    model = responses_model(enable_image_generation=True, image_generation_model="codex/gpt-image-2")
    requests = []

    def endpoint(request):
        requests.append(json.loads(request.content))
        return httpx.Response(502, json={"error": {"message": "upstream unavailable"}})

    generate, = build_image_tools("default", "image-session")
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        model.root_async_client._client = client
        with pytest.raises(openai.APIStatusError, match="upstream unavailable"):
            await generate.ainvoke({"prompt": "An orange circle"})
    assert requests == [{"model": "codex/gpt-image-2", "prompt": "An orange circle", "n": 1}]
