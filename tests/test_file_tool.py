"""read_text / write_text / edit_file / list_dir / search_files /
find_files（filesystem tool）单测：读写往返、父目录自动创建、覆盖语义、
分页与截断护栏、超限拒绝、精确替换编辑（唯一匹配/未找到/多处/全替换）、
目录列举排序与截断、内容检索（glob/大小写/正则/跳过目录/命中截断、
max_matches 只能调小）、按名查找，以及 registry 层面的注册归属
（toolset: filesystem）。
"""

import json
from unittest.mock import patch

from atoms.tools import file_tool  # noqa: F401 -- module import 即注册
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
    target = tmp_path / "sub" / "note.md"      # 父目录不存在 → 自动创建
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
    # 目录排前、各自按名称排序
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
    # .git / __pycache__ 目录被跳过（大小写敏感：markdown 里的 needle 不中）
    assert all(".git" not in p and "__pycache__" not in p for p in hit_paths)
    assert r["truncated"] is False
    line = next(m for m in r["matches"] if m["path"].endswith("a.py"))
    assert line["line"] == 2 and "NEEDLE" in line["text"]


def test_search_ignore_case_glob_and_max_matches(tmp_path):
    _make_tree(tmp_path)
    r = _run("search_files",
             {"query": "needle", "path": str(tmp_path), "ignore_case": True})
    hit_paths = {m["path"] for m in r["matches"]}
    assert str(tmp_path / "src" / "notes.md") in hit_paths  # 大小写不敏感后命中

    r = _run("search_files",
             {"query": "needle", "path": str(tmp_path),
              "ignore_case": True, "glob": "*.py"})
    assert all(m["path"].endswith(".py") for m in r["matches"])

    # args 只能调小：max_matches=1 → 恰一条 + truncated 标记
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
    # 正则命中：只匹配 foo 行
    r = _run("search_files", {"query": r"^f\w+ = 2$", "path": str(tmp_path),
                              "regex": True, "glob": "*.py"})
    assert {m["line"] for m in r["matches"]} == {2}
    r = _run("search_files", {"query": r"[unclosed(", "path": str(tmp_path),
                              "regex": True})
    assert "正则" in r["error"]
    # ignore_case 与 regex 组合
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
    # new_str 空串 = 删除（留下空行是精确替换的自然结果）
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
    # 超大文件拒绝
    r = _run("edit_file", {"path": str(target), "old_str": "h",
                           "new_str": "H"},
             guard={**_GUARD, "max_edit_chars": 3})
    assert "编辑上限" in r["error"]
    # 二进制拒绝
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"a\x00b")
    r = _run("edit_file", {"path": str(blob), "old_str": "a", "new_str": "b"})
    assert "二进制" in r["error"]


def test_edit_file_rejects_non_utf8_without_corrupting(tmp_path):
    # 非 UTF-8（GBK 等）严格解码拒绝，且文件字节原样保留——errors=replace
    # 回写会把整个文件固化成 U+FFFD 乱码，不可逆
    target = tmp_path / "gbk.txt"
    raw = "姓名:张三\nkeep me here\n".encode("gbk")
    target.write_bytes(raw)
    r = _run("edit_file", {"path": str(target), "old_str": "keep me",
                           "new_str": "keep us"})
    assert "UTF-8" in r["error"]
    assert target.read_bytes() == raw       # 未被写坏


def test_edit_file_cap_counts_chars_not_bytes(tmp_path):
    # 上限按解码后的字符数计：8 个字符的 CJK 文件占 12+ 字节，字节口径
    # 会在约 1/3 阈值处误拒
    target = tmp_path / "cjk.txt"
    target.write_text("中文内容abcd", encoding="utf-8")   # 8 字符 / 12 字节
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

    # *.py 按相对路径匹配 = 任意深度
    r = _run("find_files", {"pattern": "*.py", "path": str(tmp_path)})
    found = {p.replace(str(tmp_path) + "/", "") for p in r["files"]}
    assert found == {"src/a.py", "src/deep/b.py", "top.py"}
    assert r["truncated"] is False
    # 顶层限定
    r = _run("find_files", {"pattern": "*.md", "path": str(tmp_path)})
    assert r["files"] == [str(tmp_path / "src" / "c.md")]
    # docs/** 形式的目录内全部
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
