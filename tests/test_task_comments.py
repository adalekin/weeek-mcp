"""Task comments, which Weeek serves only from its web API."""

import pytest

from weeek_mcp import tools


def _stored(text: str) -> dict:
    """A comment as the API hands it back: the document sits under ``content.data``."""
    return {
        "id": 263,
        "sentAt": "2026-08-11T20:52:00Z",
        "user": {"name": "Алексей Далекин"},
        "content": {
            "data": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            },
            "version": 1,
            "mentions": [],
        },
    }


class FakeKB:
    def __init__(self, comments=None):
        self.comments = comments or []
        self.posted: list[tuple[int, str]] = []

    async def list_task_comments(self, task_id):
        self.posted.append(("list", task_id))
        return self.comments

    async def add_task_comment(self, task_id, markdown):
        self.posted.append((task_id, markdown))
        return _stored(markdown)

    async def update_task_comment(self, task_id, comment_id, markdown):
        self.posted.append((task_id, comment_id, markdown))
        return _stored(markdown)

    async def delete_task_comment(self, task_id, comment_id):
        self.posted.append(("delete", task_id, comment_id))


@pytest.mark.asyncio
async def test_list_task_comments_renders_the_document_as_text():
    kb = FakeKB([_stored("ничего не понятно")])

    result = await tools.handle_task_tool("weeek_list_task_comments", {"task_id": 691}, api=None, kb=kb)

    assert result == [
        {
            "id": 263,
            "author": "Алексей Далекин",
            "sent_at": "2026-08-11T20:52:00Z",
            "text": "ничего не понятно",
        }
    ]


@pytest.mark.asyncio
async def test_add_task_comment_passes_the_markdown_through():
    kb = FakeKB()

    result = await tools.handle_task_tool(
        "weeek_add_task_comment",
        {"task_id": 691, "text": "не воспроизвелось"},
        api=None,
        kb=kb,
    )

    assert kb.posted == [(691, "не воспроизвелось")]
    assert result == {"id": 263, "text": "не воспроизвелось"}


@pytest.mark.asyncio
async def test_update_task_comment_rewrites_the_same_comment():
    kb = FakeKB()

    result = await tools.handle_task_tool(
        "weeek_update_task_comment",
        {"task_id": 691, "comment_id": 263, "text": "уточнение"},
        api=None,
        kb=kb,
    )

    assert kb.posted == [(691, 263, "уточнение")]
    assert result == {"id": 263, "text": "уточнение"}


@pytest.mark.asyncio
async def test_delete_task_comment_reports_what_it_removed():
    kb = FakeKB()

    result = await tools.handle_task_tool(
        "weeek_delete_task_comment",
        {"task_id": 691, "comment_id": 263},
        api=None,
        kb=kb,
    )

    assert kb.posted == [("delete", 691, 263)]
    assert result == {"deleted": 263}


@pytest.mark.asyncio
async def test_commenting_without_a_session_says_what_to_set_up():
    with pytest.raises(ValueError, match="WEEEK_EMAIL"):
        await tools.handle_task_tool(
            "weeek_add_task_comment",
            {"task_id": 691, "text": "…"},
            api=None,
            kb=None,
        )
