"""Export destination handling (no live backend).

The server runs with its own checkout as the working directory, so a relative
``target_dir`` used to create the tree inside the repository instead of where
the caller meant it.
"""

import pytest

from weeek_mcp.config import Config
from weeek_mcp.kb.client import KBError, WeeekKB


@pytest.fixture
def kb(monkeypatch, tmp_path):
    monkeypatch.setenv("WEEEK_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("WEEEK_WORKSPACE_ID", "1")
    return WeeekKB(Config.from_env())


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

    async def no_docs(*args, **kwargs):
        return []

    monkeypatch.setattr(kb, "list_documents", no_docs)

    root = tmp_path / "kb-export"
    result = await kb.export_documents(str(root))

    assert result == {"exported": 0, "directory": str(root), "files": []}
    assert root.is_dir()


async def test_tilde_target_dir_is_accepted(kb, monkeypatch, tmp_path):
    """``~`` expands to an absolute path, so it passes the check."""
    monkeypatch.setenv("HOME", str(tmp_path))

    async def no_docs(*args, **kwargs):
        return []

    monkeypatch.setattr(kb, "list_documents", no_docs)

    result = await kb.export_documents("~/kb-export")

    assert result["directory"] == str(tmp_path / "kb-export")
    assert (tmp_path / "kb-export").is_dir()
