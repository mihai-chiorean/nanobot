"""Tests for NotebookEditTool — Jupyter .ipynb cell editing (ported, MIT-1031).

The functional cell-edit tests below are ported from the 0.2.x
``feat/shared-rooms`` implementation and re-targeted at the 0.3.0 layout
(``_FsTool`` base, ``ToolResult`` errors, workspace-confined resolution via
``_resolve_write``, which routes through ``_resolve_with_extra`` +
``is_sensitive_path`` so the MIT-121 credential guard is on the write path).
The original tool registered through a hardcoded ``loop.py`` ``register()``;
here it is discovered automatically via the ``pkgutil`` ``ToolLoader`` — see
``test_notebook_tool_registration.py`` for the loader/registration + room-policy
gate, which is the behaviour the cutover actually changed.

The 0.2.x companion change made ``edit_file`` *refuse* ``.ipynb`` and route the
user to ``notebook_edit``. 0.3.0 deliberately dropped that refusal: ``edit_file``
now edits notebooks as JSON (see ``test_edit_enhancements.TestEditIpynbFiles`` /
``test_file_edit_coding_enhancements.test_edit_file_can_edit_ipynb_as_json``).
The two tools therefore coexist; the co-existence + file-state coherence cases
at the bottom of this file pin that shared, non-duplicated behaviour.
"""

import json

import pytest

from nanobot.agent.tools import file_state
from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool
from nanobot.agent.tools.notebook import NotebookEditTool


@pytest.fixture(autouse=True)
def _clear_file_state():
    """Reset the global read-state map between tests."""
    file_state.clear()
    yield
    file_state.clear()


def _make_notebook(cells: list[dict] | None = None, nbformat: int = 4, nbformat_minor: int = 5) -> dict:
    """Build a minimal valid .ipynb structure."""
    return {
        "nbformat": nbformat,
        "nbformat_minor": nbformat_minor,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
        "cells": cells or [],
    }


def _code_cell(source: str, cell_id: str | None = None) -> dict:
    cell = {"cell_type": "code", "source": source, "metadata": {}, "outputs": [], "execution_count": None}
    if cell_id:
        cell["id"] = cell_id
    return cell


def _md_cell(source: str, cell_id: str | None = None) -> dict:
    cell = {"cell_type": "markdown", "source": source, "metadata": {}}
    if cell_id:
        cell["id"] = cell_id
    return cell


def _write_nb(tmp_path, name: str, nb: dict) -> str:
    p = tmp_path / name
    p.write_text(json.dumps(nb), encoding="utf-8")
    return str(p)


class TestNotebookEdit:

    @pytest.fixture()
    def tool(self, tmp_path):
        return NotebookEditTool(workspace=tmp_path)

    @pytest.mark.asyncio
    async def test_replace_cell_content(self, tool, tmp_path):
        nb = _make_notebook([_code_cell("print('hello')"), _code_cell("x = 1")])
        path = _write_nb(tmp_path, "test.ipynb", nb)
        result = await tool.execute(path=path, cell_index=0, new_source="print('world')")
        assert "Successfully" in result
        saved = json.loads((tmp_path / "test.ipynb").read_text())
        assert saved["cells"][0]["source"] == "print('world')"
        assert saved["cells"][1]["source"] == "x = 1"

    @pytest.mark.asyncio
    async def test_insert_cell_after_target(self, tool, tmp_path):
        nb = _make_notebook([_code_cell("cell 0"), _code_cell("cell 1")])
        path = _write_nb(tmp_path, "test.ipynb", nb)
        result = await tool.execute(path=path, cell_index=0, new_source="inserted", edit_mode="insert")
        assert "Successfully" in result
        saved = json.loads((tmp_path / "test.ipynb").read_text())
        assert len(saved["cells"]) == 3
        assert saved["cells"][0]["source"] == "cell 0"
        assert saved["cells"][1]["source"] == "inserted"
        assert saved["cells"][2]["source"] == "cell 1"

    @pytest.mark.asyncio
    async def test_delete_cell(self, tool, tmp_path):
        nb = _make_notebook([_code_cell("A"), _code_cell("B"), _code_cell("C")])
        path = _write_nb(tmp_path, "test.ipynb", nb)
        result = await tool.execute(path=path, cell_index=1, edit_mode="delete")
        assert "Successfully" in result
        saved = json.loads((tmp_path / "test.ipynb").read_text())
        assert len(saved["cells"]) == 2
        assert saved["cells"][0]["source"] == "A"
        assert saved["cells"][1]["source"] == "C"

    @pytest.mark.asyncio
    async def test_create_new_notebook_from_scratch(self, tool, tmp_path):
        path = str(tmp_path / "new.ipynb")
        result = await tool.execute(path=path, cell_index=0, new_source="# Hello", edit_mode="insert", cell_type="markdown")
        assert "Successfully" in result or "created" in result.lower()
        saved = json.loads((tmp_path / "new.ipynb").read_text())
        assert saved["nbformat"] == 4
        assert len(saved["cells"]) == 1
        assert saved["cells"][0]["cell_type"] == "markdown"
        assert saved["cells"][0]["source"] == "# Hello"

    @pytest.mark.asyncio
    async def test_invalid_cell_index_error(self, tool, tmp_path):
        nb = _make_notebook([_code_cell("only cell")])
        path = _write_nb(tmp_path, "test.ipynb", nb)
        result = await tool.execute(path=path, cell_index=5, new_source="x")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_non_ipynb_rejected(self, tool, tmp_path):
        f = tmp_path / "script.py"
        f.write_text("pass")
        result = await tool.execute(path=str(f), cell_index=0, new_source="x")
        assert "Error" in result
        assert ".ipynb" in result

    @pytest.mark.asyncio
    async def test_preserves_metadata_and_outputs(self, tool, tmp_path):
        cell = _code_cell("old")
        cell["outputs"] = [{"output_type": "stream", "text": "hello\n"}]
        cell["execution_count"] = 42
        nb = _make_notebook([cell])
        path = _write_nb(tmp_path, "test.ipynb", nb)
        await tool.execute(path=path, cell_index=0, new_source="new")
        saved = json.loads((tmp_path / "test.ipynb").read_text())
        assert saved["metadata"]["kernelspec"]["language"] == "python"

    @pytest.mark.asyncio
    async def test_nbformat_45_generates_cell_id(self, tool, tmp_path):
        nb = _make_notebook([], nbformat_minor=5)
        path = _write_nb(tmp_path, "test.ipynb", nb)
        await tool.execute(path=path, cell_index=0, new_source="x = 1", edit_mode="insert")
        saved = json.loads((tmp_path / "test.ipynb").read_text())
        assert "id" in saved["cells"][0]
        assert len(saved["cells"][0]["id"]) > 0

    @pytest.mark.asyncio
    async def test_insert_with_cell_type_markdown(self, tool, tmp_path):
        nb = _make_notebook([_code_cell("code")])
        path = _write_nb(tmp_path, "test.ipynb", nb)
        await tool.execute(path=path, cell_index=0, new_source="# Title", edit_mode="insert", cell_type="markdown")
        saved = json.loads((tmp_path / "test.ipynb").read_text())
        assert saved["cells"][1]["cell_type"] == "markdown"

    @pytest.mark.asyncio
    async def test_invalid_edit_mode_rejected(self, tool, tmp_path):
        nb = _make_notebook([_code_cell("code")])
        path = _write_nb(tmp_path, "test.ipynb", nb)
        result = await tool.execute(path=path, cell_index=0, new_source="x", edit_mode="replcae")
        assert "Error" in result
        assert "edit_mode" in result

    @pytest.mark.asyncio
    async def test_invalid_cell_type_rejected(self, tool, tmp_path):
        nb = _make_notebook([_code_cell("code")])
        path = _write_nb(tmp_path, "test.ipynb", nb)
        result = await tool.execute(path=path, cell_index=0, new_source="x", cell_type="raw")
        assert "Error" in result
        assert "cell_type" in result


class TestNotebookEditErrorPaths:
    """Errors are ``ToolResult``-valued (``is_error`` True) — the 0.3.0 contract.

    The 0.2.x version returned bare ``"Error: ..."`` strings; the cutover
    requires the structured form so failure detection is not substring-based.
    """

    @pytest.fixture()
    def tool(self, tmp_path):
        return NotebookEditTool(workspace=tmp_path)

    @pytest.mark.asyncio
    async def test_missing_path_is_error_not_result(self, tool):
        result = await tool.execute(cell_index=0, new_source="x")
        assert isinstance(result, object)
        assert getattr(result, "is_error", False) is True
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_non_ipynb_is_error(self, tool, tmp_path):
        f = tmp_path / "plain.txt"
        f.write_text("hello")
        result = await tool.execute(path=str(f), cell_index=0, new_source="x")
        assert getattr(result, "is_error", False) is True
        assert ".ipynb" in result

    @pytest.mark.asyncio
    async def test_unreadable_file_reports_error_not_silent_none(self, tool, tmp_path):
        # A directory passed as path makes read_text raise IsADirectoryError —
        # the real exception path must surface as a structured error, never a
        # silently-None result.
        d = tmp_path / "folder.ipynb"
        d.mkdir()
        result = await tool.execute(path=str(d), cell_index=0, new_source="x")
        assert getattr(result, "is_error", False) is True
        assert result != ""

    @pytest.mark.asyncio
    async def test_malformed_notebook_reports_error(self, tool, tmp_path):
        # Not a valid notebook JSON document -> parse error, structured.
        p = tmp_path / "broken.ipynb"
        p.write_text("{ this is not json", encoding="utf-8")
        result = await tool.execute(path=str(p), cell_index=0, new_source="x", edit_mode="delete")
        assert getattr(result, "is_error", False) is True
        assert "Error" in result


class TestNotebookEditWorkspaceConfinement:
    """``_resolve_write`` must confine targets to the tool workspace (PR4).

    Built the way the production loader builds it for a restricted workspace
    (``allowed_dir=workspace``), so the boundary is actually enforced —
    resolution goes through ``_resolve_with_extra`` + ``is_sensitive_path``,
    never a bare ``os.path.expanduser`` / ``Path()`` of the raw model-supplied
    string, and never an ``open()`` on it before the containment check.
    """

    @pytest.mark.asyncio
    async def test_path_traversal_denied_outside_workspace(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        secret = tmp_path / "secret.ipynb"
        secret.write_text(json.dumps(_make_notebook([_code_cell("secret")])), encoding="utf-8")
        tool = NotebookEditTool(workspace=workspace, allowed_dir=workspace)
        # Reach for a file outside the workspace via a relative traversal.
        result = await tool.execute(path="../secret.ipynb", cell_index=0, new_source="pwned")
        assert getattr(result, "is_error", False) is True
        # The out-of-workspace target must be untouched.
        assert json.loads(secret.read_text())["cells"][0]["source"] == "secret"

    @pytest.mark.asyncio
    async def test_absolute_path_outside_workspace_denied(self, tmp_path):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside.ipynb"
        outside.write_text(json.dumps(_make_notebook([_code_cell("keep")])), encoding="utf-8")
        tool = NotebookEditTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path=str(outside), cell_index=0, new_source="pwned")
        assert getattr(result, "is_error", False) is True
        assert json.loads(outside.read_text())["cells"][0]["source"] == "keep"

    @pytest.mark.asyncio
    async def test_sensitive_path_denied_before_write(self, tmp_path):
        # A credential-shaped target must be refused by is_sensitive_path
        # before any write is attempted.  The fixture lives INSIDE the guarded
        # workspace (under a real ``.ssh`` dir, whose name the matcher keys on)
        # so the workspace boundary check passes and only the sensitive-path
        # guard can deny it — proving the guard, not the boundary, fired.
        workspace = tmp_path / "ws"
        (workspace / ".ssh").mkdir(parents=True)
        raw = str(workspace / ".ssh" / "id_rsa.ipynb")
        tool = NotebookEditTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path=raw, cell_index=0, new_source="pwned", edit_mode="insert")
        assert getattr(result, "is_error", False) is True
        # The raw credential path must NOT have been written from.
        assert not (workspace / ".ssh" / "id_rsa.ipynb").exists()


class TestNotebookEditCoexistence:
    """Both tools coexist on the same workspace and share the file-state store.

    ``edit_file`` keeps the 0.3.0 ``.ipynb``-as-JSON behaviour (no refusal);
    ``notebook_edit`` performs the cell write and records it, so a subsequent
    ``edit_file`` sees a coherent (already-read, fresh-mtime) state rather than
    a spurious "have not read this file yet" warning.
    """

    @pytest.fixture()
    def store(self):
        return file_state.FileStates()

    @pytest.mark.asyncio
    async def test_edit_file_still_edits_ipynb_as_json_no_refusal(self, tmp_path, store):
        # The dropped-guard behaviour: edit_file must NOT refuse .ipynb.
        nb = _make_notebook([_code_cell("print(1)")])
        path = _write_nb(tmp_path, "nb.ipynb", nb)
        edit = EditFileTool(workspace=tmp_path, file_states=store)
        result = await edit.execute(
            path=path,
            old_text='"source": "print(1)"',
            new_text='"source": "print(2)"',
            replace_all=True,
        )
        assert "Successfully" in result
        # The dropped-guard behaviour: no refusal, no routing to notebook_edit.
        assert "refuse" not in str(result).lower()
        assert "use the notebook" not in str(result).lower()
        assert "print(2)" in (tmp_path / "nb.ipynb").read_text()

    @pytest.mark.asyncio
    async def test_notebook_edit_then_edit_file_coherent(self, tmp_path, store):
        # Real sequence: notebook_edit (cell write, records the write) then
        # edit_file (text replacement) on the SAME file.  The edit_file read-
        # before-edit check must pass without a spurious warning because
        # notebook_edit recorded the write via the shared store.
        nb = _make_notebook([_code_cell("a = 1"), _code_cell("b = 2")])
        path = _write_nb(tmp_path, "nb.ipynb", nb)
        nb_tool = NotebookEditTool(workspace=tmp_path, file_states=store)
        edit_tool = EditFileTool(workspace=tmp_path, file_states=store)

        r1 = await nb_tool.execute(path=path, cell_index=0, new_source="a = 99")
        assert "Successfully" in r1

        r2 = await edit_tool.execute(
            path=path,
            old_text='"source": "b = 2"',
            new_text='"source": "b = 100"',
            replace_all=True,
        )
        assert "Successfully edited" in r2
        # No staleness warning leaked into the result.
        assert "Warning" not in r2
        saved = json.loads((tmp_path / "nb.ipynb").read_text())
        assert saved["cells"][0]["source"] == "a = 99"
        assert "b = 100" in (tmp_path / "nb.ipynb").read_text()

    @pytest.mark.asyncio
    async def test_edit_file_then_notebook_edit_coherent(self, tmp_path, store):
        # Reverse order: read_file then edit_file (records write) then
        # notebook_edit reads the file the model already read via the shared
        # store; the notebook tool's own read is internal and must succeed and
        # must not emit a not-read warning.
        nb = _make_notebook([_code_cell("x = 1")])
        path = _write_nb(tmp_path, "nb.ipynb", nb)
        read_tool = ReadFileTool(workspace=tmp_path, file_states=store)
        edit_tool = EditFileTool(workspace=tmp_path, file_states=store)
        nb_tool = NotebookEditTool(workspace=tmp_path, file_states=store)

        rr = await read_tool.execute(path=path)
        assert getattr(rr, "is_error", False) is False
        re_ = await edit_tool.execute(
            path=path,
            old_text='"source": "x = 1"',
            new_text='"source": "x = 2"',
            replace_all=True,
        )
        assert "Successfully edited" in re_
        rn = await nb_tool.execute(path=path, cell_index=0, new_source="x = 3")
        assert "Successfully" in rn
        assert "Warning" not in rn
        assert json.loads((tmp_path / "nb.ipynb").read_text())["cells"][0]["source"] == "x = 3"

    @pytest.mark.asyncio
    async def test_deleted_file_cannot_be_notebook_edited(self, tmp_path, store):
        # After the file is deleted, a notebook_edit must fail (File not found),
        # not resurrect or silently write through the freed path.
        nb = _make_notebook([_code_cell("a")])
        path = _write_nb(tmp_path, "gone.ipynb", nb)
        tool = NotebookEditTool(workspace=tmp_path, file_states=store)
        (tmp_path / "gone.ipynb").unlink()
        result = await tool.execute(path=path, cell_index=0, new_source="x")
        assert getattr(result, "is_error", False) is True
        assert "File not found" in result
