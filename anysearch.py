#!/usr/bin/env python3
"""anysearch1 —— 纯标准库实现的本地文本搜索命令行工具。

设计目标是 grep 风格的可用子集：
  * 递归搜索目录，按 glob 过滤文件、按目录名剪枝；
  * 支持普通字符串 / 正则、大小写敏感开关；
  * 支持 -A / -B / -C 上下文行；
  * 输出人类可读结果或 --json 结构化结果；
  * 退出码遵循 grep 约定：0 有匹配、1 无匹配、2 出错。

示例::

    python anysearch.py "def main" . --include "*.py" -C 2
    python anysearch.py -i -E "todo|fixme" src --json
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

__version__ = "1.0.0"

#: 默认剪枝的目录名（避免搜索进依赖、版本库和缓存目录）。
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
)

#: 判定"二进制文件"时嗅探的字节数。
BINARY_SNIFF_BYTES = 8192

#: 单个文件的默认大小上限（字节），超过则跳过，避免误扫大文件。
DEFAULT_MAX_FILE_SIZE = 5 * 1024 * 1024


@dataclass
class Match:
    """一条命中记录。行号从 1 开始，``path`` 为相对于搜索根目录的路径。"""

    path: str
    line: int
    text: str
    before: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "line": self.line,
            "text": self.text,
            "before": list(self.before),
            "after": list(self.after),
        }


@dataclass
class SearchReport:
    """一次完整搜索的结果汇总。"""

    root: str
    pattern: str
    matches: list[Match] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0

    @property
    def matched_files(self) -> int:
        return len({m.path for m in self.matches})

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "pattern": self.pattern,
            "matches": [m.to_dict() for m in self.matches],
            "stats": {
                "matches": len(self.matches),
                "matched_files": self.matched_files,
                "files_scanned": self.files_scanned,
                "files_skipped": self.files_skipped,
                "truncated": self.truncated,
                "elapsed_ms": round(self.elapsed_ms, 3),
            },
        }


def build_pattern(
    query: str, *, regex: bool = False, ignore_case: bool = False
) -> re.Pattern[str]:
    """把查询串编译成正则。非正则模式下按字面量匹配（转义元字符）。"""
    source = query if regex else re.escape(query)
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(source, flags)
    except re.error as exc:  # pragma: no cover - 由 CLI 层转成友好报错
        raise ValueError(f"无效的正则表达式 {query!r}: {exc}") from exc


def is_binary_file(path: Path, sniff: int = BINARY_SNIFF_BYTES) -> bool:
    """通过嗅探 NUL 字节粗略判断是否二进制文件。"""
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(sniff)
    except OSError:
        return True


def iter_files(
    root: Path,
    *,
    includes: Sequence[str] = (),
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    follow_symlinks: bool = False,
) -> Iterator[Path]:
    """深度优先遍历 ``root``，产出通过过滤的普通文件（顺序稳定，便于复现）。"""
    if root.is_file():
        yield root
        return

    exclude_set = set(excludes)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        # 原地修改 dirnames 才能让 os.walk 真正剪枝。
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_set)
        for name in sorted(filenames):
            if name in exclude_set:
                continue
            if includes and not any(
                fnmatch.fnmatch(name, pattern) for pattern in includes
            ):
                continue
            candidate = Path(dirpath) / name
            if candidate.is_symlink() and not follow_symlinks:
                continue
            if not candidate.is_file():
                continue
            yield candidate


def search_file(
    path: Path,
    pattern: re.Pattern[str],
    *,
    display_path: str,
    before: int = 0,
    after: int = 0,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> list[Match]:
    """在单个文件里搜索，返回命中列表（含上下文行）。"""
    try:
        if path.stat().st_size > max_file_size:
            return []
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = raw.splitlines()
    hits: list[Match] = []
    for index, line in enumerate(lines):
        if not pattern.search(line):
            continue
        hits.append(
            Match(
                path=display_path,
                line=index + 1,
                text=line,
                before=lines[max(0, index - before) : index] if before else [],
                after=lines[index + 1 : index + 1 + after] if after else [],
            )
        )
    return hits


def search(
    query: str,
    root: str | os.PathLike[str] = ".",
    *,
    regex: bool = False,
    ignore_case: bool = False,
    includes: Sequence[str] = (),
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    context: int = 0,
    before: int | None = None,
    after: int | None = None,
    limit: int | None = None,
    follow_symlinks: bool = False,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> SearchReport:
    """在目录中搜索 ``query``，返回 :class:`SearchReport`。

    ``context`` 同时指定上下文字行数，``before`` / ``after`` 可单独覆盖。
    """
    started = time.perf_counter()
    root_path = Path(root)
    report = SearchReport(root=str(root_path), pattern=query)

    if not root_path.exists():
        raise FileNotFoundError(f"路径不存在: {root_path}")

    ctx_before = context if before is None else before
    ctx_after = context if after is None else after
    compiled = build_pattern(query, regex=regex, ignore_case=ignore_case)

    base = root_path if root_path.is_dir() else root_path.parent
    for candidate in iter_files(
        root_path, includes=includes, excludes=excludes, follow_symlinks=follow_symlinks
    ):
        if is_binary_file(candidate):
            report.files_skipped += 1
            continue
        report.files_scanned += 1
        try:
            display_path = str(candidate.relative_to(base)) or candidate.name
        except ValueError:  # pragma: no cover - 极少见的跨盘/相对路径异常
            display_path = str(candidate)
        report.matches.extend(
            search_file(
                candidate,
                compiled,
                display_path=display_path,
                before=ctx_before,
                after=ctx_after,
                max_file_size=max_file_size,
            )
        )
        if limit is not None and len(report.matches) >= limit:
            report.truncated = True
            report.matches = report.matches[:limit]
            break

    report.elapsed_ms = (time.perf_counter() - started) * 1000
    return report


def _highlight(text: str, pattern: re.Pattern[str], enabled: bool) -> str:
    if not enabled or not pattern.pattern:
        return text
    return pattern.sub(lambda m: f"\033[1;31m{m.group(0)}\033[0m", text)


def format_text(report: SearchReport, *, color: bool = False) -> str:
    """把搜索结果渲染成 grep 风格文本。"""
    pattern = build_pattern(report.pattern)
    out: list[str] = []
    current: str | None = None
    for match in report.matches:
        if match.path != current:
            if current is not None:
                out.append("--")
            current = match.path
        elif match.before or match.after:
            out.append("--")
        for offset, ctx in enumerate(match.before):
            out.append(f"{match.path}-{match.line - len(match.before) + offset}- {ctx}")
        out.append(
            f"{match.path}:{match.line}: "
            f"{_highlight(match.text, pattern, color)}"
        )
        for offset, ctx in enumerate(match.after):
            out.append(f"{match.path}-{match.line + offset + 1}- {ctx}")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anysearch",
        description="anysearch1 —— 本地文本搜索工具（纯标准库实现）",
    )
    parser.add_argument("query", help="要搜索的内容（默认按字面量匹配）")
    parser.add_argument("path", nargs="?", default=".", help="搜索目录或文件，默认当前目录")
    parser.add_argument("-E", "--regex", action="store_true", help="把 query 当正则表达式")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="忽略大小写")
    parser.add_argument(
        "-A", "--after", type=int, default=None, metavar="N", help="额外打印后 N 行"
    )
    parser.add_argument(
        "-B", "--before", type=int, default=None, metavar="N", help="额外打印前 N 行"
    )
    parser.add_argument("-C", "--context", type=int, default=0, metavar="N", help="打印上下 N 行")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="只搜索匹配该 glob 的文件，可重复",
    )
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        metavar="NAME",
        help="额外剪枝的目录名，可重复",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="不再剪枝 .git / node_modules 等默认目录",
    )
    parser.add_argument("-l", "--limit", type=int, default=None, metavar="N", help="最多输出 N 条命中")
    parser.add_argument("--json", action="store_true", help="输出结构化 JSON")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--follow-symlinks", action="store_true", help="跟随符号链接")
    parser.add_argument("--version", action="version", version=f"anysearch {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    excludes = [] if args.no_default_excludes else list(DEFAULT_EXCLUDES)
    excludes.extend(args.exclude_dir)

    try:
        report = search(
            args.query,
            args.path,
            regex=args.regex,
            ignore_case=args.ignore_case,
            includes=args.include,
            excludes=excludes,
            context=args.context,
            before=args.before,
            after=args.after,
            limit=args.limit,
            follow_symlinks=args.follow_symlinks,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"anysearch: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        use_color = args.color == "always" or (
            args.color == "auto" and sys.stdout.isatty()
        )
        rendered = format_text(report, color=use_color)
        if rendered:
            print(rendered)
        print(
            f"[anysearch] {len(report.matches)} 处命中 / "
            f"{report.matched_files} 个文件 / 扫描 {report.files_scanned} 个文件 / "
            f"{report.elapsed_ms:.1f} ms",
            file=sys.stderr,
        )

    return 0 if report.matches else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
