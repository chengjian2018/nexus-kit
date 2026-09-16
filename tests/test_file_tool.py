"""Unit tests for read_text / write_text / edit_file / list_dir /
search_files / find_files (filesystem tools): read/write round-trip,
automatic parent-directory creation, overwrite semantics, pagination and
truncation guards, oversized-content rejection, exact-replacement edits
(unique match / not found / multiple matches / replace_all), directory
listing ordering and truncation, content search (glob / case /
regex / skipped directories / hit truncation, max_matches can only be
lowered), find-by-name, robustness guards (FIFO/device read refusal,
read byte budget, walk time budget stopped_early, exact cap not counted
as truncated), plus registry-level registration ownership
(toolset: filesystem).
"""

import json
import os
from unittest.mock import patch

from atoms.tools import file_tool  # noqa: F401 -- registers on module import
from nexus.registry.tools import registry as tool_registry

_GUARD = {"max_read_chars": 50000, "max_write_chars": 200000,
          "max_list_entries": 500, "max_matches": 100,
          "max_edit_chars": 200000, "max_find_results": 500}


def _run(name, args, guard=None):
    from async_utils import arun
    with patch("atoms.tools.file_tool.get_file_tool_config",
               return_value=dict(guard or _GUARD)):
        return json.loads(arun(tool_registry.dispatch(name, args)))


def test_registered_in_filesystem_toolset():
    assert tool_registry.get_toolset_for_tool("read_text") == "filesystem"
    assert tool_registry.get_toolset_for_tool("write_text") == "filesystem"
    assert tool_registry.get_toolset_for_tool("edit_file") == "filesystem"
    assert tool_registry.get_toolset_for_tool("list_dir") == "filesystem"
    assert tool_registry.get_toolset_for_tool("search_files") == "filesystem"
    assert tool_registry.get_toolset_for_tool("find_files") == "filesystem"
    assert {"read_text", "write_text", "edit_file", "list_dir",
            "search_files", "find_files"} <= \
        tool_registry.names_in_toolsets({"filesystem"})


# ---------------------------------------------------------------------------
# write_text / read_text
# ---------------------------------------------------------------------------

def test_write_then_read_roundtrip(tmp_path):
    target = tmp_path / "sub" / "note.md"      # missing parent directory -> created automatically
    r = _run("write_text", {"path": str(target), "content": "hello\nworld"})
    assert r["created"] is True
    assert r["chars_written"] == len("hello\nworld")
    assert target.read_text(encoding="utf-8") == "hello\nworld"

    r = _run("read_text", {"path": str(target)})
    assert r["content"] == "hello\nworld"
    assert r["total_lines"] == 2
    assert r["lines_read"] == 2
    assert r["truncated"] is False


def test_write_overwrites_existing(tmp_path):
    target = tmp_path / "f.txt"
    _run("write_text", {"path": str(target), "content": "old"})
    r = _run("write_text", {"path": str(target), "content": "new"})
    assert r["created"] is False
    assert target.read_text(encoding="utf-8") == "new"


def test_write_rejects_oversized_content(tmp_path):
    r = _run("write_text",
             {"path": str(tmp_path / "big.txt"), "content": "x" * 100},
             guard={**_GUARD, "max_write_chars": 10})
    assert "超过写入上限" in r["error"]
    assert not (tmp_path / "big.txt").exists()


def test_write_rejects_directory_target_and_missing_content(tmp_path):
    r = _run("write_text", {"path": str(tmp_path), "content": "x"})
    assert "目录" in r["error"]
    r = _run("write_text", {"path": str(tmp_path / "f.txt")})
    assert "content" in r["error"]


def test_read_missing_and_directory(tmp_path):
    r = _run("read_text", {"path": str(tmp_path / "nope.txt")})
    assert "不存在" in r["error"]
    r = _run("read_text", {"path": str(tmp_path)})
    assert "目录" in r["error"]


def test_read_rejects_binary(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"abc\x00def")
    r = _run("read_text", {"path": str(target)})
    assert "二进制" in r["error"]


def test_read_offset_limit_pagination(tmp_path):
    target = tmp_path / "lines.txt"
    target.write_text("\n".join(f"line{i}" for i in range(10)),
                      encoding="utf-8")
    r = _run("read_text", {"path": str(target), "offset": 2, "limit": 3})
    assert r["content"] == "line2\nline3\nline4"
    assert r["total_lines"] == 10
    assert r["offset"] == 2
    assert r["lines_read"] == 3


def test_read_char_truncation(tmp_path):
    target = tmp_path / "long.txt"
    target.write_text("x" * 500, encoding="utf-8")
    r = _run("read_text", {"path": str(target)},
             guard={**_GUARD, "max_read_chars": 100})
    assert r["truncated"] is True
    assert len(r["content"]) < 250
    assert "截断" in r["content"]


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------

def test_list_dir_ordering_types_and_truncation(tmp_path):
    (tmp_path / "b_file.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "z_dir").mkdir()
    (tmp_path / "a_file.txt").write_text("yy", encoding="utf-8")

    r = _run("list_dir", {"path": str(tmp_path)})
    names = [e["name"] for e in r["entries"]]
    # directories first, each group sorted by name
    assert names == ["a_dir", "z_dir", "a_file.txt", "b_file.txt"]
    kinds = {e["name"]: e["type"] for e in r["entries"]}
    assert kinds["a_dir"] == "dir" and kinds["a_file.txt"] == "file"
    assert r["truncated"] is False and r["total"] == 4

    r = _run("list_dir", {"path": str(tmp_path)},
             guard={**_GUARD, "max_list_entries": 2})
    assert r["truncated"] is True
    assert len(r["entries"]) == 2
    assert r["total"] == 4


def test_list_dir_errors(tmp_path):
    r = _run("list_dir", {"path": str(tmp_path / "nope")})
    assert "不存在" in r["error"]
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    r = _run("list_dir", {"path": str(target)})
    assert "不是目录" in r["error"]


# ---------------------------------------------------------------------------
# search_files
# ---------------------------------------------------------------------------

def _make_tree(root):
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("def foo():\n    return NEEDLE\n",
                                       encoding="utf-8")
    (root / "src" / "b.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "notes.md").write_text("needle in markdown\n",
                                           encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "a.py").write_text("NEEDLE should be skipped\n",
                                        encoding="utf-8")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "a.cpython.pyc").write_text(
        "NEEDLE binary-ish\n", encoding="utf-8")


def test_search_substring_and_skip_dirs(tmp_path):
    _make_tree(tmp_path)
    r = _run("search_files", {"query": "NEEDLE", "path": str(tmp_path)})
    hit_paths = {m["path"] for m in r["matches"]}
    assert str(tmp_path / "src" / "a.py") in hit_paths
    # .git / __pycache__ directories skipped (case-sensitive: the needle in markdown does not match)
    assert all(".git" not in p and "__pycache__" not in p for p in hit_paths)
    assert r["truncated"] is False
    line = next(m for m in r["matches"] if m["path"].endswith("a.py"))
    assert line["line"] == 2 and "NEEDLE" in line["text"]


def test_search_ignore_case_glob_and_max_matches(tmp_path):
    _make_tree(tmp_path)
    r = _run("search_files",
             {"query": "needle", "path": str(tmp_path), "ignore_case": True})
    hit_paths = {m["path"] for m in r["matches"]}
    assert str(tmp_path / "src" / "notes.md") in hit_paths  # hits once case-insensitive

    r = _run("search_files",
             {"query": "needle", "path": str(tmp_path),
              "ignore_case": True, "glob": "*.py"})
    assert all(m["path"].endswith(".py") for m in r["matches"])

    # args can only lower it: max_matches=1 -> exactly one hit + truncated flag
    r = _run("search_files",
             {"query": "needle", "path": str(tmp_path),
              "ignore_case": True, "max_matches": 1})
    assert len(r["matches"]) == 1 and r["truncated"] is True


def test_search_errors(tmp_path):
    r = _run("search_files", {"query": ""})
    assert "query" in r["error"]
    r = _run("search_files", {"query": "x", "path": str(tmp_path / "nope")})
    assert "根目录" in r["error"]


def test_search_regex_mode(tmp_path):
    (tmp_path / "r.py").write_text("x1 = 1\nfoo = 2\nbar42 = 3\n",
                                   encoding="utf-8")
    # regex hit: only the foo line matches
    r = _run("search_files", {"query": r"^f\w+ = 2$", "path": str(tmp_path),
                              "regex": True, "glob": "*.py"})
    assert {m["line"] for m in r["matches"]} == {2}
    r = _run("search_files", {"query": r"[unclosed(", "path": str(tmp_path),
                              "regex": True})
    assert "正则" in r["error"]
    # ignore_case combined with regex
    r = _run("search_files", {"query": r"^FOO", "path": str(tmp_path),
                              "regex": True, "ignore_case": True})
    assert any(m["line"] == 2 for m in r["matches"])


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------

def test_edit_file_unique_replace(tmp_path):
    target = tmp_path / "code.py"
    target.write_text("def foo():\n    return 1\n\nprint(foo())\n",
                      encoding="utf-8")
    r = _run("edit_file", {"path": str(target),
                           "old_str": "    return 1",
                           "new_str": "    return 42"})
    assert r["replacements"] == 1
    assert "return 42" in target.read_text(encoding="utf-8")
    assert "return 1\n" not in target.read_text(encoding="utf-8")


def test_edit_file_not_found_and_ambiguous(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("aaa\nbbb\naaa\n", encoding="utf-8")
    r = _run("edit_file", {"path": str(target), "old_str": "zzz",
                           "new_str": "x"})
    assert "未找到" in r["error"]
    r = _run("edit_file", {"path": str(target), "old_str": "aaa",
                           "new_str": "x"})
    assert "2 次" in r["error"] and "replace_all" in r["error"]


def test_edit_file_replace_all_and_delete(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("aaa\nbbb\naaa\n", encoding="utf-8")
    r = _run("edit_file", {"path": str(target), "old_str": "aaa",
                           "new_str": "x", "replace_all": True})
    assert r["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "x\nbbb\nx\n"
    # empty new_str = deletion (the leftover empty line is a natural result of exact replacement)
    r = _run("edit_file", {"path": str(target), "old_str": "bbb",
                           "new_str": ""})
    assert target.read_text(encoding="utf-8") == "x\n\nx\n"


def test_edit_file_guards(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("hello\n", encoding="utf-8")
    r = _run("edit_file", {"path": str(target), "old_str": "",
                           "new_str": "x"})
    assert "old_str" in r["error"]
    r = _run("edit_file", {"path": str(target), "old_str": "hello",
                           "new_str": "hello"})
    assert "相同" in r["error"]
    r = _run("edit_file", {"path": str(tmp_path / "nope"), "old_str": "a",
                           "new_str": "b"})
    assert "不存在" in r["error"]
    # oversized file rejected
    r = _run("edit_file", {"path": str(target), "old_str": "h",
                           "new_str": "H"},
             guard={**_GUARD, "max_edit_chars": 3})
    assert "编辑上限" in r["error"]
    # binary rejected
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"a\x00b")
    r = _run("edit_file", {"path": str(blob), "old_str": "a", "new_str": "b"})
    assert "二进制" in r["error"]


def test_edit_file_rejects_non_utf8_without_corrupting(tmp_path):
    # non-UTF-8 (GBK etc.) rejected via strict decoding, file bytes kept
    # intact — an errors=replace rewrite would bake the whole file into
    # irreversible U+FFFD mojibake
    target = tmp_path / "gbk.txt"
    raw = "姓名:张三\nkeep me here\n".encode("gbk")
    target.write_bytes(raw)
    r = _run("edit_file", {"path": str(target), "old_str": "keep me",
                           "new_str": "keep us"})
    assert "UTF-8" in r["error"]
    assert target.read_bytes() == raw       # not corrupted


def test_edit_file_cap_counts_chars_not_bytes(tmp_path):
    # the cap counts decoded characters: an 8-char CJK file takes 12+ bytes,
    # a byte-based measure would falsely reject at about 1/3 of the threshold
    target = tmp_path / "cjk.txt"
    target.write_text("中文内容abcd", encoding="utf-8")   # 8 chars / 12 bytes
    r = _run("edit_file", {"path": str(target), "old_str": "abcd",
                           "new_str": "ABC"},
             guard={**_GUARD, "max_edit_chars": 10})
    assert "path" in r and target.read_text(encoding="utf-8") == "中文内容ABC"


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------

def test_find_files_glob_and_skips(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "deep" / "b.py").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "c.md").write_text("x", encoding="utf-8")
    (tmp_path / "top.py").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hidden.py").write_text("x", encoding="utf-8")

    # *.py matches relative paths = any depth
    r = _run("find_files", {"pattern": "*.py", "path": str(tmp_path)})
    found = {p.replace(str(tmp_path) + "/", "") for p in r["files"]}
    assert found == {"src/a.py", "src/deep/b.py", "top.py"}
    assert r["truncated"] is False
    # top-level restriction
    r = _run("find_files", {"pattern": "*.md", "path": str(tmp_path)})
    assert r["files"] == [str(tmp_path / "src" / "c.md")]
    # "docs/**"-style: everything inside the directory
    r = _run("find_files", {"pattern": "src/deep/*", "path": str(tmp_path)})
    assert r["files"] == [str(tmp_path / "src" / "deep" / "b.py")]


def test_find_files_guards(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.py").write_text("x", encoding="utf-8")
    r = _run("find_files", {"pattern": "*.py", "path": str(tmp_path),
                            "max_results": 2})
    assert len(r["files"]) == 2 and r["truncated"] is True
    r = _run("find_files", {"pattern": " ", "path": str(tmp_path)})
    assert "pattern" in r["error"]
    r = _run("find_files", {"pattern": "*.py",
                            "path": str(tmp_path / "nope")})
    assert "根目录" in r["error"]


# ---------------------------------------------------------------------------
# Robustness guards: non-regular-file read refusal / read byte budget / walk time budget / exact cap
# ---------------------------------------------------------------------------

def test_read_rejects_fifo_and_device(tmp_path):
    # a FIFO can fool the exists/is_dir checks; only open blocks forever —
    # stat runs first and refuses; this test finishing at all proves there
    # was no hang (to_thread threads cannot be cancelled)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    r = _run("read_text", {"path": str(fifo)})
    assert "普通文件" in r["error"]
    if os.path.exists("/dev/zero"):     # present on both darwin and linux
        r = _run("read_text", {"path": "/dev/zero"})
        assert "普通文件" in r["error"]


def test_read_byte_budget_rejects_oversized(tmp_path):
    # byte budget = max_read_chars * 4 + 1024 (a UTF-8 char takes at most 4 bytes)
    big = tmp_path / "big.txt"
    big.write_text("中" * 400, encoding="utf-8")    # 1200 bytes > 10*4+1024
    r = _run("read_text", {"path": str(big)},
             guard={**_GUARD, "max_read_chars": 10})
    assert "上限" in r["error"]
    # multi-byte content within budget reads normally
    ok = tmp_path / "ok.txt"
    ok.write_text("中文ok", encoding="utf-8")       # 8 bytes
    r = _run("read_text", {"path": str(ok)},
             guard={**_GUARD, "max_read_chars": 10})
    assert r["content"] == "中文ok" and r["truncated"] is False


def test_search_and_find_deadline_marks_stopped_early(tmp_path):
    # negative budget -> deadline already passed, stops at the first checkpoint
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    with patch.object(file_tool, "_WALK_TIME_BUDGET_SECONDS", -1.0):
        r = _run("search_files", {"query": "needle", "path": str(tmp_path)})
    assert r["stopped_early"] is True and r["truncated"] is True
    assert r["matches"] == []
    with patch.object(file_tool, "_WALK_TIME_BUDGET_SECONDS", -1.0):
        r = _run("find_files", {"pattern": "*.py", "path": str(tmp_path)})
    assert r["stopped_early"] is True and r["truncated"] is True
    assert r["files"] == []


def test_search_exact_cap_not_truncated(tmp_path):
    # exactly 1 hit + max_matches=1: nothing more -> not truncated
    (tmp_path / "one.py").write_text("hit\nplain\n", encoding="utf-8")
    r = _run("search_files", {"query": "hit", "path": str(tmp_path),
                              "max_matches": 1})
    assert len(r["matches"]) == 1 and r["truncated"] is False
    # a second hit is the evidence of "there is more"
    (tmp_path / "two.py").write_text("hit again\n", encoding="utf-8")
    r = _run("search_files", {"query": "hit", "path": str(tmp_path),
                              "max_matches": 1})
    assert len(r["matches"]) == 1 and r["truncated"] is True


def test_find_files_exact_cap_not_truncated(tmp_path):
    (tmp_path / "only.py").write_text("x", encoding="utf-8")
    r = _run("find_files", {"pattern": "*.py", "path": str(tmp_path),
                            "max_results": 1})
    assert len(r["files"]) == 1 and r["truncated"] is False
    (tmp_path / "second.py").write_text("x", encoding="utf-8")
    r = _run("find_files", {"pattern": "*.py", "path": str(tmp_path),
                            "max_results": 1})
    assert len(r["files"]) == 1 and r["truncated"] is True
