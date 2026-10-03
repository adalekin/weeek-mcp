"""Moving and reordering knowledge base documents.

Weeek has one write for this, ``PATCH /kb/hierarchy {targetId, placeId, direction}``,
and one quirk: moving a document that has children reverses their order at every
level of the subtree. The fake below plays the same game so the compensation can
be checked offline.
"""

import httpx
import pytest

from weeek_mcp.config import Config
from weeek_mcp.kb.client import KBError, WeeekKB, _error_text
from weeek_mcp.tools import handle_kb_tool


class FakeHierarchy:
    def __init__(self, *rows: tuple[int, int | None, str], reverse: bool = True):
        self.by = {i: {"id": i, "parentId": p, "name": n, "sort": k} for k, (i, p, n) in enumerate(rows)}
        self.reverse = reverse
        self.patches: list[dict] = []
        self.trash: list[int] = []
        self.hidden: set[int] = set()  # known to Weeek, absent from search

    def kids(self, parent: int | None) -> list[dict]:
        return sorted((a for a in self.by.values() if a["parentId"] == parent), key=lambda a: a["sort"])

    def names(self, parent: int | None) -> str:
        return "".join(a["name"] for a in self.kids(parent))

    def _restack(self, parent: int | None, order: list[dict]) -> None:
        for k, a in enumerate(order):
            a["parentId"], a["sort"] = parent, k

    def patch(self, body: dict) -> dict:
        self.patches.append(body)
        target, place = self.by[body["targetId"]], self.by[body["placeId"]]
        if body["direction"] == "into":
            parent, siblings, at = place["id"], [a for a in self.kids(place["id"]) if a is not target], 0
        else:
            parent = place["parentId"]
            siblings = [a for a in self.kids(parent) if a is not target]
            at = siblings.index(place) + (body["direction"] == "down")
        siblings.insert(at, target)
        self._restack(parent, siblings)
        if self.reverse:
            stack = [target["id"]]
            while stack:
                folder = stack.pop()
                kids = self.kids(folder)
                self._restack(folder, kids[::-1])
                stack.extend(a["id"] for a in kids)
        return {"success": True, "data": {"articles": [{"id": target["id"], "parentId": parent, "sort": 0}]}}


@pytest.fixture
def make_kb(monkeypatch, tmp_path):
    monkeypatch.setenv("WEEEK_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("WEEEK_WORKSPACE_ID", "1")

    def make(fake: FakeHierarchy) -> WeeekKB:
        client = WeeekKB(Config.from_env())

        async def fake_get(path, params=None, **kwargs):
            if path == "/app/avatars":
                return {"data": {}}
            if path == "/ws/1/kb/hierarchy":
                return {"data": {"trash": fake.trash}}
            if path != "/ws/1/kb/articles/search":
                return {"article": fake.by[int(path.rsplit("/", 1)[1])]}
            rows = [dict(a) for a in fake.by.values() if a["id"] not in fake.hidden]
            return {"articles": rows[params["offset"] : params["offset"] + params["limit"]]}

        async def fake_patch(path, payload):
            assert path == "/ws/1/kb/hierarchy"
            return fake.patch(payload)

        async def fake_post(path, payload):
            assert path == "/ws/1/kb/articles"
            new_id = max(fake.by) + 1
            fake.by[new_id] = {"id": new_id, "parentId": None, "name": payload["name"], "sort": -new_id}
            return {"article": {"id": new_id}}

        monkeypatch.setattr(client, "_get", fake_get)
        monkeypatch.setattr(client, "_post", fake_post)
        monkeypatch.setattr(client, "_patch", fake_patch)
        return client

    return make


# Two top-level folders: F holds A, B, C; A holds D, E. G is empty.
def tree(**kw) -> FakeHierarchy:
    return FakeHierarchy(
        (1, None, "F"),
        (2, 1, "A"),
        (4, 2, "D"),
        (5, 2, "E"),
        (3, 1, "B"),
        (6, 1, "C"),
        (7, None, "G"),
        **kw,
    )


@pytest.mark.parametrize(("arg", "direction"), [("parent_id", "into"), ("before", "up"), ("after", "down")])
async def test_each_placement_maps_to_its_direction(make_kb, arg, direction):
    fake = tree()
    await make_kb(fake).move_document("6", **{arg: "3"})
    assert fake.patches == [{"targetId": 6, "placeId": 3, "direction": direction}]


async def test_reorders_siblings(make_kb):
    fake = tree()
    assert await make_kb(fake).move_document("6", before="2") == {"parent_id": "1"}
    assert fake.names(1) == "CAB"


async def test_moving_to_the_top_level_reports_no_parent(make_kb):
    fake = tree()
    assert await make_kb(fake).move_document("3", after="7") == {"parent_id": None}
    assert fake.names(None) == "FGB"


@pytest.mark.parametrize("kwargs", [{}, {"parent_id": "7", "after": "3"}, {"before": "2", "after": "3"}])
async def test_exactly_one_placement_is_required_before_any_request(make_kb, kwargs):
    fake = tree()
    with pytest.raises(KBError, match="exactly one"):
        await make_kb(fake).move_document("6", **kwargs)
    assert fake.patches == []


async def test_a_document_cannot_go_next_to_itself(make_kb):
    fake = tree()
    with pytest.raises(KBError, match="itself"):
        await make_kb(fake).move_document("6", after="6")
    assert fake.patches == []


async def test_a_folder_cannot_go_inside_its_own_subtree(make_kb):
    fake = tree()
    with pytest.raises(KBError, match="inside"):
        await make_kb(fake).move_document("1", after="4")
    assert fake.patches == []


async def test_the_reversed_children_of_a_moved_folder_are_put_back(make_kb):
    fake = tree()
    assert await make_kb(fake).move_document("1", after="7") == {"parent_id": None}
    assert fake.names(None) == "GF"
    assert fake.names(1) == "ABC"
    assert fake.names(2) == "DE"  # the nested level too
    assert len(fake.patches) == 2


async def test_a_leaf_is_moved_once(make_kb):
    fake = tree()
    await make_kb(fake).move_document("6", parent_id="7")
    assert len(fake.patches) == 1


async def test_a_folder_whose_order_survived_is_not_moved_again(make_kb):
    fake = tree(reverse=False)
    await make_kb(fake).move_document("1", after="7")
    assert len(fake.patches) == 1


async def test_order_that_cannot_be_restored_is_reported(make_kb):
    fake = tree()
    flips = iter([True, False])  # the second move leaves the order alone
    original = fake.patch

    def patch(body):
        fake.reverse = next(flips)
        return original(body)

    fake.patch = patch  # type: ignore[method-assign]
    result = await make_kb(fake).move_document("1", after="7")
    assert "warning" in result


async def test_list_is_depth_first_in_sidebar_order(make_kb):
    fake = tree()
    fake.by[6]["sort"] = -1  # C ahead of everything under F
    fake.by[9] = {"id": 9, "parentId": 404, "name": "Orphan", "sort": 100}  # parent not visible
    docs = await make_kb(fake).list_documents()
    assert [d.title for d in docs] == ["F", "C", "A", "D", "E", "B", "G", "Orphan"]
    assert [d.parent_id for d in docs][:3] == [None, "1", "1"]


async def test_the_tree_is_read_past_the_first_page(make_kb, monkeypatch):
    """A KB larger than one search page: the children of a moved folder may sit on any page."""
    monkeypatch.setattr("weeek_mcp.kb.client._PAGE_LIMIT", 2)
    fake = tree()
    kb = make_kb(fake)
    assert len(await kb.list_documents()) == 7
    await kb.move_document("1", after="7")
    assert fake.names(1) == "ABC"
    assert fake.names(2) == "DE"


async def test_tool_returns_the_new_parent(make_kb):
    fake = tree()
    result = await handle_kb_tool("weeek_kb_move", {"doc_id": "3", "before": "1"}, make_kb(fake))
    assert result == {"id": "3", "parent_id": None, "moved": True}


async def test_documents_created_in_a_folder_keep_the_order_they_were_created_in(make_kb):
    fake = tree()
    kb = make_kb(fake)
    for title in "XY":
        await kb.create_document(title, parent_id="1")
    assert fake.names(1) == "ABCXY"


async def test_the_first_document_in_an_empty_folder_goes_into_it(make_kb):
    fake = tree()
    doc = await make_kb(fake).create_document("X", parent_id="7")
    assert fake.patches == [{"targetId": int(doc.id), "placeId": 7, "direction": "into"}]
    assert fake.names(7) == "X"


async def test_a_trashed_document_is_no_place_to_move_to(make_kb):
    fake = tree()
    fake.trash = [99]
    with pytest.raises(KBError, match="trash"):
        await make_kb(fake).move_document("6", after="99")
    assert fake.patches == []


async def test_nothing_is_created_under_a_trashed_parent(make_kb):
    fake = tree()
    fake.trash = [99]
    with pytest.raises(KBError, match="trash"):
        await make_kb(fake).create_document("X", parent_id="99")
    assert len(fake.by) == 7


async def test_a_document_search_does_not_list_is_not_taken_for_trashed(make_kb):
    """Private documents are missing from search too; only the trash list refuses."""
    fake = tree()
    fake.by[50] = {"id": 50, "parentId": None, "name": "Private", "sort": 50}
    fake.hidden = {50}
    await make_kb(fake).move_document("6", parent_id="50")
    assert fake.names(50) == "C"


@pytest.mark.parametrize(
    ("order", "result", "moves"),
    [
        (["6", "3", "2"], "CBA", 2),  # full reversal
        (["2", "3", "6"], "ABC", 0),  # already in place
        (["6"], "CAB", 1),  # the rest follow in their current order
        (["3", "2"], "BAC", 1),
    ],
)
async def test_reorder_lines_documents_up(make_kb, order, result, moves):
    fake = tree()
    out = await make_kb(fake).reorder_documents(order)
    assert fake.names(1) == result
    assert out["moved"] == moves == len(fake.patches)
    assert out["parent_id"] == "1"
    assert "".join(fake.by[int(i)]["name"] for i in out["order"]) == result


async def test_reorder_works_at_the_top_level_and_keeps_what_is_inside(make_kb):
    fake = tree()
    out = await make_kb(fake).reorder_documents(["7", "1"])
    assert out["parent_id"] is None
    assert fake.names(None) == "GF"
    assert fake.names(1) == "ABC"
    assert fake.names(2) == "DE"
    assert "warning" not in out


@pytest.mark.parametrize(
    ("order", "message"),
    [(["2", "7"], "one folder"), (["2", "2"], "more than once"), (["2", "404"], "404")],
)
async def test_reorder_refuses_a_bad_list_before_any_request(make_kb, order, message):
    fake = tree()
    with pytest.raises(KBError, match=message):
        await make_kb(fake).reorder_documents(order)
    assert fake.patches == []


@pytest.mark.parametrize(("parent", "titles"), [("1", "ABC"), ("", "FG"), ("7", "")])
async def test_list_can_be_narrowed_to_one_folder(make_kb, parent, titles):
    docs = await handle_kb_tool("weeek_kb_list", {"parent_id": parent}, make_kb(tree()))
    assert "".join(d["title"] for d in docs) == titles


def test_an_api_error_shows_the_message_weeek_sent():
    body = b'{"success":false,"code":4000003,"message":"\\u041d\\u0435\\u043b\\u044c\\u0437\\u044f"}'
    assert _error_text(httpx.Response(400, content=body)) == "Нельзя (code 4000003)"
    assert _error_text(httpx.Response(502, content=b"<html>Bad Gateway</html>")) == "<html>Bad Gateway</html>"
