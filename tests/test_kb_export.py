"""Export destination and mirroring (no live backend).

Two things used to go wrong. The server runs with its own checkout as the working
directory, so a relative ``target_dir`` built the tree inside the repository. And
the export only ever wrote, so renamed, moved and deleted documents left their old
files behind and the folder drifted away from the KB it mirrors.
"""

import pytest

from weeek_mcp.config import Config
from weeek_mcp.kb.client import KBDocument, KBError, WeeekKB


@pytest.fixture
def kb(monkeypatch, tmp_path):
    monkeypatch.setenv("WEEEK_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("WEEEK_WORKSPACE_ID", "1")
    return WeeekKB(Config.from_env())


def _doc(doc_id: str, title: str, *folders: str) -> KBDocument:
    """A document sitting under ``folders``; the trail ends with the doc itself."""
    crumbs = (*folders, title)
    return KBDocument(id=doc_id, title=title, path=" / ".join(crumbs), crumbs=crumbs)


def _serve(kb, monkeypatch, docs):
    """Serve a fixed set of documents with a one-line body each."""

    async def listing(*args, **kwargs):
        return docs

    async def read(doc_id):
        return f"body of {doc_id}"

    monkeypatch.setattr(kb, "list_documents", listing)
    monkeypatch.setattr(kb, "search", listing)
    monkeypatch.setattr(kb, "read_document", read)


# ------------------------------------------------------------- destination


@pytest.mark.parametrize(
    "target",
    ["projects/approck/utopy-kb-export", "export", "./export", "../export"],
)
async def test_relative_target_dir_is_refused(kb, monkeypatch, tmp_path, target):
    """A relative path is rejected before anything is listed or created."""
    monkeypatch.chdir(tmp_path)

    async def fail(*args, **kwargs):
        raise AssertionError("documents were listed despite the bad target_dir")

    monkeypatch.setattr(kb, "list_documents", fail)
    monkeypatch.setattr(kb, "search", fail)

    with pytest.raises(KBError) as excinfo:
        await kb.export_documents(target)

    assert "absolute" in str(excinfo.value)
    assert list(tmp_path.iterdir()) == []  # nothing written where the server stands


async def test_absolute_target_dir_is_accepted(kb, monkeypatch, tmp_path):
    """An absolute path still exports, and the folder is created for it."""
    _serve(kb, monkeypatch, [])

    root = tmp_path / "kb-export"
    result = await kb.export_documents(str(root))

    assert result == {
        "exported": 0,
        "removed": 0,
        "directory": str(root),
        "files": [],
        "pruned": [],
    }
    assert root.is_dir()


async def test_tilde_target_dir_is_accepted(kb, monkeypatch, tmp_path):
    """``~`` expands to an absolute path, so it passes the check."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _serve(kb, monkeypatch, [])

    result = await kb.export_documents("~/kb-export")

    assert result["directory"] == str(tmp_path / "kb-export")
    assert (tmp_path / "kb-export").is_dir()


# --------------------------------------------------------------- mirroring


async def test_rename_does_not_leave_the_old_file(kb, monkeypatch, tmp_path):
    """The document keeps its id, so the file under the old title must go."""
    root = tmp_path / "export"

    _serve(kb, monkeypatch, [_doc("164", "F12: Old name", "Features")])
    await kb.export_documents(str(root))
    assert (root / "Features" / "F12- Old name.md").exists()

    _serve(kb, monkeypatch, [_doc("164", "E12.1: New name", "Features")])
    result = await kb.export_documents(str(root))

    assert not (root / "Features" / "F12- Old name.md").exists()
    assert (root / "Features" / "E12.1- New name.md").exists()
    assert result["removed"] == 1
    assert [p.name for p in root.rglob("*.md")] == ["E12.1- New name.md"]


async def test_move_does_not_leave_the_old_folder(kb, monkeypatch, tmp_path):
    """A document that changed parents leaves neither file nor empty folder."""
    root = tmp_path / "export"

    _serve(kb, monkeypatch, [_doc("160", "M11.1: Hookup", "Drafts", "E6: Video")])
    await kb.export_documents(str(root))
    assert (root / "Drafts" / "E6- Video" / "M11.1- Hookup.md").exists()

    _serve(kb, monkeypatch, [_doc("160", "M11.1: Hookup", "Models", "M11: Flux")])
    await kb.export_documents(str(root))

    assert (root / "Models" / "M11- Flux" / "M11.1- Hookup.md").exists()
    assert not (root / "Drafts").exists()  # emptied by the move, so it goes too


async def test_deleted_document_loses_its_file(kb, monkeypatch, tmp_path):
    """A document gone from the KB must not survive in the mirror."""
    root = tmp_path / "export"

    _serve(kb, monkeypatch, [_doc("13", "Providers", "Tech"), _doc("24", "Roadmap")])
    await kb.export_documents(str(root))

    _serve(kb, monkeypatch, [_doc("24", "Roadmap")])
    result = await kb.export_documents(str(root))

    assert result["exported"] == 1
    assert result["removed"] == 1
    assert result["pruned"] == [str(root / "Tech" / "Providers.md")]
    assert not (root / "Tech").exists()


async def test_foreign_files_are_left_alone(kb, monkeypatch, tmp_path):
    """Anything without our front matter belongs to the user."""
    root = tmp_path / "export"
    root.mkdir()
    (root / "my notes.md").write_text("# mine\n", encoding="utf-8")
    (root / "README.txt").write_text("mine too\n", encoding="utf-8")
    scratch = root / "scratch"
    scratch.mkdir()
    (scratch / "thoughts.md").write_text("still mine\n", encoding="utf-8")

    _serve(kb, monkeypatch, [_doc("24", "Roadmap")])
    result = await kb.export_documents(str(root))

    assert result["removed"] == 0
    assert (root / "my notes.md").exists()
    assert (root / "README.txt").exists()
    assert (scratch / "thoughts.md").exists()


async def test_a_filtered_export_prunes_nothing(kb, monkeypatch, tmp_path):
    """A query returns a subset, which is no evidence the rest is stale."""
    root = tmp_path / "export"

    _serve(kb, monkeypatch, [_doc("24", "Roadmap"), _doc("85", "Metrics")])
    await kb.export_documents(str(root))

    _serve(kb, monkeypatch, [_doc("24", "Roadmap")])
    result = await kb.export_documents(str(root), query="road")

    assert result["removed"] == 0
    assert (root / "Metrics.md").exists()


async def test_hidden_folders_are_never_swept(kb, monkeypatch, tmp_path):
    """A snapshot of an earlier export is full of our front matter — and is not ours."""
    root = tmp_path / "export"

    _serve(kb, monkeypatch, [_doc("13", "Providers", "Tech"), _doc("24", "Roadmap")])
    await kb.export_documents(str(root))

    # The kind of copy someone keeps before a risky run.
    backup = root / ".before-changes"
    backup.mkdir()
    (backup / "Providers.md").write_text("---\nweeek_id: 13\n---\n\nold body\n", encoding="utf-8")
    nested = backup / "Tech"
    nested.mkdir()
    (nested / "Roadmap.md").write_text("---\nweeek_id: 24\n---\n\nold body\n", encoding="utf-8")

    _serve(kb, monkeypatch, [_doc("24", "Roadmap")])
    result = await kb.export_documents(str(root))

    assert result["removed"] == 1  # only the live stale file, none of the backup
    assert result["pruned"] == [str(root / "Tech" / "Providers.md")]
    assert (backup / "Providers.md").exists()
    assert (nested / "Roadmap.md").exists()


async def test_a_legacy_file_in_the_target_folder_does_not_stop_the_export(kb, monkeypatch, tmp_path):
    """Before we passed an encoding, exports were written in the host locale.

    A Windows box is left holding cp1251 files; reading them back as strict UTF-8
    would abort the very export that is supposed to replace them.
    """
    root = tmp_path / "export"
    root.mkdir()
    legacy = root / "Roadmap.md"
    legacy.write_bytes("---\ntitle: Roadmap\nweeek_id: 24\n---\n\nстарое тело\n".encode("cp1251"))

    _serve(kb, monkeypatch, [_doc("24", "Roadmap")])
    result = await kb.export_documents(str(root))

    assert result["exported"] == 1
    assert legacy.read_text(encoding="utf-8").endswith("body of 24")


async def test_a_legacy_file_for_a_gone_document_is_still_pruned(kb, monkeypatch, tmp_path):
    """The stamp is ASCII, so a garbled decode still identifies the file as ours."""
    root = tmp_path / "export"
    root.mkdir()
    stale = root / "Providers.md"
    stale.write_bytes("---\nweeek_id: 13\n---\n\nстарое тело\n".encode("cp1251"))
    mine = root / "notes.md"
    mine.write_bytes("мои заметки\n".encode("cp1251"))

    _serve(kb, monkeypatch, [_doc("24", "Roadmap")])
    result = await kb.export_documents(str(root))

    assert result["pruned"] == [str(stale)]
    assert mine.exists()
