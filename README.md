# anysearch1

一个**纯标准库**实现的本地文本搜索命令行工具，grep 常用子集的轻量替代品。
零依赖，一个文件即全部实现，直接丢进任何 Python 3.9+ 环境就能跑。

```bash
python anysearch.py "def main" . --include "*.py" -A 1
```

## 特性

- 递归搜索目录；按目录名剪枝（默认跳过 `.git`、`node_modules`、`__pycache__`、`.venv` 等）
- 按 glob 过滤文件：`--include "*.py"`，可重复传入
- 字面量匹配（默认，自动转义正则元字符）或正则匹配：`-E`
- 大小写不敏感：`-i`
- 上下文行：`-C N` / `-A N` / `-B N`
- 命中数量上限：`-l N`
- 人类可读输出或结构化 JSON：`--json`
- 退出码遵循 grep 约定：`0` 有命中、`1` 无命中、`2` 出错（便于写进 CI / shell 管道）
- 自动跳过二进制文件（NUL 字节嗅探）和超大文件（默认 5 MB 上限）

## 用法

```
python anysearch.py [-h] [-E] [-i] [-A N] [-B N] [-C N] [--include GLOB]
                    [--exclude-dir NAME] [--no-default-excludes] [-l N]
                    [--json] [--color {auto,always,never}] [--follow-symlinks]
                    [--version] query [path]
```

### 示例

搜索所有 Python 文件里的 `def search` 并打印后一行：

```console
$ python anysearch.py "def search" . --include "*.py" -A 1
anysearch.py:161: def search_file(
anysearch.py-162-     path: Path,
--
anysearch.py:195: def search(
anysearch.py-196-     query: str,
--
[anysearch] 2 处命中 / 1 个文件 / 扫描 3 个文件 / 0.8 ms
```

大小写不敏感 + 正则 + JSON 输出：

```console
$ python anysearch.py -i -E "todo|fixme" . --include "*.py" --json
{
  "root": ".",
  "pattern": "todo|fixme",
  "matches": [
    {
      "path": "anysearch.py",
      "line": 14,
      "text": "    python anysearch.py -i -E \"todo|fixme\" src --json",
      "before": [],
      "after": []
    }
  ],
  "stats": {
    "matches": 1,
    "matched_files": 1,
    "files_scanned": 3,
    "files_skipped": 0,
    "truncated": false,
    "elapsed_ms": 0.494
  }
}
```

## 作为库使用

```python
from anysearch import search, format_text

report = search("TODO", "src", includes=["*.py"], context=2, ignore_case=True)
print(report.matched_files, len(report.matches), report.elapsed_ms)
print(format_text(report))
```

`search()` 返回 `SearchReport`，其中 `matches` 是 `Match(path, line, text, before, after)`
列表，`to_dict()` 可直接喂给 `json.dumps`。

## 测试

```bash
python -m unittest discover -s tests -t .
```

17 个用例，覆盖正则构建、字面量转义、目录剪枝、二进制跳过、上下文行、
数量上限、退出码和 JSON 输出。

## 设计说明

- **为什么不用 `rg`？** 这份实现的目标是"零依赖 + 可嵌入"：在不方便装二进制的
  Windows 主机、容器镜像或 CI 里，拷一个 `.py` 文件就能用，同时把逻辑暴露成
  可复用的 `search()` 函数，而不是只能靠子进程调用。
- **为什么默认按字面量匹配？** 用户搜索 `foo(bar)` 时，预期是搜这段文本本身。
  需要模式匹配时显式加 `-E`，避免"意外的正则"。
- **上下文行的实现**：整体读入文件并按行号索引，因此对源码文件（KB 级）足够快，
  也不会像流式实现那样在处理重叠上下文组时出错；超大文件由 `max_file_size` 挡掉。

## 许可

MIT，见 [LICENSE](LICENSE)。
