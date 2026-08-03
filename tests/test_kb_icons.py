"""Document icon resolution against a stubbed avatar catalog (no live backend)."""

import pytest

from weeek_mcp.config import Config
from weeek_mcp.kb.client import KBError, WeeekKB

CATALOG = {
    "success": True,
    "data": {
        "icons": [
            {"id": "icon-fire", "name": "fire"},
            {"id": "icon-warning", "name": "Warning"},
            {"id": "icon-warning-dup", "name": "Warning"},  # the real catalog has duplicates
        ],
        "emojiGroups": [
            {
                "name": "Travel & Places",
                "emojis": [
                    {"id": "emoji-rocket", "name": "Rocket", "unicode": "U+1F680"},
                    {"id": "emoji-comet", "name": "Comet", "unicode": "U+2604 U+FE0F"},
                ],
            }
        ],
    },
}


@pytest.fixture
def kb(monkeypatch, tmp_path):
    monkeypatch.setenv("WEEEK_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("WEEEK_WORKSPACE_ID", "1")
    client = WeeekKB(Config.from_env())

    async def fake_get(path, **kwargs):
        assert path == "/app/avatars"
        return CATALOG

    monkeypatch.setattr(client, "_get", fake_get)
    return client


async def test_resolves_emoji_character(kb):
    assert await kb._resolve_icon("🚀") == {"objectType": "emoji", "objectId": "emoji-rocket"}


async def test_variation_selector_is_optional(kb):
    with_vs = await kb._resolve_icon("☄️")
    without_vs = await kb._resolve_icon("☄")
    assert with_vs == without_vs == {"objectType": "emoji", "objectId": "emoji-comet"}


async def test_resolves_icon_name_case_insensitively(kb):
    assert await kb._resolve_icon("FIRE") == {"objectType": "icon", "objectId": "icon-fire"}


async def test_unknown_icon_lists_the_options(kb):
    with pytest.raises(KBError) as exc:
        await kb._resolve_icon("sparkling unicorn")
    assert "fire" in str(exc.value)


async def test_icon_names_are_deduplicated_and_sorted_for_a_reader(kb):
    assert await kb.icon_names() == ["fire", "Warning"]


async def test_icon_label_names_what_a_document_carries(kb):
    await kb._load_catalog()
    assert kb._icon_label({"objectType": "emoji", "objectId": "emoji-rocket"}) == "🚀"
    assert kb._icon_label({"objectType": "icon", "objectId": "icon-fire"}) == "fire"
    assert kb._icon_label({"objectType": "upload", "objectId": "whatever"}) is None
    assert kb._icon_label(None) is None


async def test_listing_survives_an_unavailable_catalog(kb, monkeypatch):
    async def failing_get(path, **kwargs):
        raise KBError("catalog is down")

    monkeypatch.setattr(kb, "_get", failing_get)
    await kb._load_catalog_quietly()
    assert kb._icon_label({"objectType": "emoji", "objectId": "emoji-rocket"}) is None


async def test_set_icon_posts_to_the_avatar_subresource(kb, monkeypatch):
    calls = []

    async def fake_post(path, payload):
        calls.append((path, payload))
        return {"success": True}

    monkeypatch.setattr(kb, "_post", fake_post)
    assert await kb.set_icon("42", "🚀") == "🚀"
    assert calls == [("/ws/1/kb/articles/42/avatar", {"objectType": "emoji", "objectId": "emoji-rocket"})]


async def test_an_unknown_icon_fails_before_the_document_is_created(kb, monkeypatch):
    posts = []

    async def fake_post(path, payload):
        posts.append(path)
        return {"article": {"id": 77, "name": "T"}}

    monkeypatch.setattr(kb, "_post", fake_post)
    with pytest.raises(KBError):
        await kb.create_document("T", icon="sparkling unicorn")
    assert posts == []  # no stray document left behind


async def test_create_document_with_an_icon_writes_both(kb, monkeypatch):
    posts = []

    async def fake_post(path, payload):
        posts.append((path, payload))
        return {"article": {"id": 77, "name": "T"}}

    monkeypatch.setattr(kb, "_post", fake_post)
    doc = await kb.create_document("T", icon="🚀")
    assert doc.icon == "🚀"
    assert posts == [
        ("/ws/1/kb/articles", {"name": "T", "content": {}}),
        ("/ws/1/kb/articles/77/avatar", {"objectType": "emoji", "objectId": "emoji-rocket"}),
    ]


async def test_catalog_is_fetched_once_even_without_built_in_icons(kb, monkeypatch):
    fetches = []

    async def emoji_only_get(path, **kwargs):
        fetches.append(path)
        return {"data": {"icons": [], "emojiGroups": CATALOG["data"]["emojiGroups"]}}

    monkeypatch.setattr(kb, "_get", emoji_only_get)
    await kb._load_catalog()
    await kb._load_catalog()
    assert len(fetches) == 1


async def test_listing_survives_a_catalog_that_is_not_even_json(kb, monkeypatch):
    async def garbage_get(path, **kwargs):
        raise ValueError("Expecting value: line 1 column 1")  # what resp.json() raises on HTML

    monkeypatch.setattr(kb, "_get", garbage_get)
    await kb._load_catalog_quietly()


async def test_empty_icon_clears_it(kb, monkeypatch):
    calls = []

    async def fake_delete(path):
        calls.append(path)
        return {"success": True}

    monkeypatch.setattr(kb, "_delete", fake_delete)
    assert await kb.set_icon("42", "") is None
    assert calls == ["/ws/1/kb/articles/42/avatar"]
