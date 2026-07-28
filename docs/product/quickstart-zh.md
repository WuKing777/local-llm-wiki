# 中文快速开始

Local LLM Wiki 是一个本地优先的文件型知识库工具。它适合整理本地资料、维护带来源证据的 wiki 页面、重建本地搜索索引，并在需要时从本地证据生成草稿。

先运行合成演示，不要直接使用真实库：

```powershell
python -B -m pip install -e .
.\tools\run-demo.ps1
python -B -m kb product-console --root "examples/demo-root" --json
python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open
```

`examples/demo-root` 是公开合成数据，只用于本地演示和烟雾检查。演示会使用 `docs/product/assets/first-run-demo.png` 所示的安全路径，并生成一个小型本地报告；报告只保留命令名、退出码、状态摘要和已脱敏输出摘要。

## 什么时候用它

- 你有本地 Markdown、文本、HTML、PDF 文本层或 OCR 派生文本，需要导入、检索、回答问题或写 wiki。
- 你需要让稳定内容保持可追溯，引用本地 source id 和精确证据。
- 你想先看产品状态，而不是直接执行写操作，可以运行 `doctor`、`schema-check` 和 `product-console`。

## 第一次真实使用

为你控制的空目录初始化一个新根目录：

```powershell
python -B -m kb init --root "<root>"
python -B -m kb doctor --root "<root>"
python -B -m kb schema-check --root "<root>" --json
python -B -m kb product-console --root "<root>" --json
```

把原始资料放进 `raw/` 或通过导入命令进入 `raw/`，source card 放在 `sources/`，稳定 wiki 放在 `wiki/`，审阅和报告放在 `meta/`，可重建索引放在 `db/`。

## 绝对不要先做这些

- 不要把演示命令指向真实库、私人资料目录、生产根目录或未备份的 Obsidian vault。
- 不要把 API key、bearer token、prompt、完整模型响应、私人源文本或私人 source chunk 写进文档、脚本、截图、报告、日志或提交。
- 不要把 LLM 输出直接写入稳定 wiki 页面。
- 不要把 provider preflight 当成真实提供商可用或真实发布已批准。

## 本地优先和证据边界

默认不会调用云端或模型提供商。`doctor` 默认不做在线探测，`product-console` 只描述本地状态和可用动作，`web-console` 只提供本机 loopback 的只读浏览器视图，演示脚本会为子进程隔离配置目录并清空常见 provider 环境变量。

AI/LLM output is never a fact source。DeepSeek 和其他 LLM 可以在明确批准后整理、总结、推理和创建草稿，但稳定内容必须经过 `validate-draft` 和 `publish-draft`，并且每个稳定 claim 都要有本地 source chunk 的精确 quote 证据。草稿不是稳定内容；稳定内容要通过验证和发布门禁。

下一步：

- 想看完整演示故事，读 [首次运行演示](first-run-demo.md)。
- 想从浏览器看本地状态，读 [本地网页控制台](local-web-console.md)。
- 想知道“我现在该运行哪个命令”，读 [命令选择指南](command-guide.md)。
- 想配置可选 provider，先读 [Provider Preflight](provider-preflight.md)。

Default demo note: `tools/run-demo.ps1` uses `examples/demo-story` to build a temp synthetic root and writes a redacted `synthetic-demo-story-v1` report. The story includes `capture-candidate`, `review-candidate`, `publish-memory`, deterministic draft validate/publish, `trust-report`, governance, backup, restore, and `migrate-check`. The intended boundary is `tracked_fixtures_mutated=false`.
