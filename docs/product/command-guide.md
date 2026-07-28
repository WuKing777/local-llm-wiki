# 命令选择指南

如果你知道自己想做什么，但不知道命令名，先看这一页。所有示例都使用占位路径；真实根目录必须由当前任务明确批准。

| 我想做什么 | 先运行哪个命令 | 说明 |
| --- | --- | --- |
| 新建一个本地知识库 | `python -B -m kb init --root "<root>"` | 创建目录结构和基础元数据。不要用真实库做演示。 |
| 健康检查 | `python -B -m kb doctor --root "<root>"` | 默认本地、离线、读操作；可报告可选依赖 warning。 |
| 检查 schema | `python -B -m kb schema-check --root "<root>" --json` | 验证 manifest、source card、wiki front matter、backup manifest、benchmark 等 contract。 |
| 看产品状态 | `python -B -m kb product-console --root "<root>" --json` | 描述本地状态和动作，不直接执行动作。 |
| 查看可信证据报告 | `python -B -m kb trust-report --root "<root>" --json` | 只读展示 source review、稳定 wiki 引用、exact quote 支持、draft validation、governance 状态和 audit 指针。 |
| 在本机浏览器查看状态 | `python -B -m kb web-console --root "<root>" --no-open` | 只绑定 loopback，显示只读状态和可复制命令，不从浏览器执行写操作。 |
| 导入单个文件 | `python -B -m kb ingest "<source-file>" --root "<root>"` | 把本地 source 导入知识库。 |
| 导入 inbox | `python -B -m kb ingest-inbox --root "<root>"` | 批量导入 inbox 中支持的本地文件。 |
| 搜索本地证据 | `python -B -m kb search "<query>" --root "<root>"` | 使用本地索引检索证据。 |
| 用本地证据回答 | `python -B -m kb answer "<question>" --root "<root>"` | 输出答案、source id 和 evidence quote。 |
| 离线检查草稿路径 | `python -B -m kb llm-preflight --root "<root>" --query "<query>" --title "<title>" --offline --json` | 不调用 provider，先检查本地上下文和边界。 |
| 验证草稿 | `python -B -m kb validate-draft --root "<root>" "<draft-path>" --target "<title>"` | 检查草稿 claim 是否被本地 quote 证据支持。 |
| 发布草稿 | `python -B -m kb publish-draft --root "<root>" "<draft-path>" --target "<title>"` | 通过验证后才写入稳定 wiki。 |
| 备份 | `python -B -m kb backup --root "<root>" --output "<backup.zip>"` | 创建允许列表内的 durable backup。 |
| 恢复 | `python -B -m kb restore --backup "<backup.zip>" --root "<restored-root>"` | 恢复到新目录，避免覆盖真实库。 |
| 校验迁移 | `python -B -m kb migrate-check --source "<root>" --restored "<restored-root>" --json` | 对比源目录和恢复目录。 |
| 捕获候选记忆 | `python -B -m kb capture-candidate --root "<root>" --type self_statement --text "<candidate text>" --event-date "2026-07-07" --privacy personal --confidence confirmed --value-reason "<why useful>" --suggested-source-type self_statement` | 创建候选，不是稳定事实。 |
| 推荐主题 | `python -B -m kb suggest-topics --root "<root>" --json` | 基于本地材料生成主题建议元数据。 |
| 生成每日工作流 | `python -B -m kb daily-workflow --root "<root>" --date "2026-07-07" --json` | 生成脱敏的本地日工作流计划。 |
| 增加检索 benchmark | `python -B -m kb benchmark-add --root "<root>" --query "<query>" --expected-source-id "src-xxxxxxxxxxxx" --privacy public --json` | 只给公开或已批准样本使用。 |
| 检查个人外脑结构 | `python -B -m kb exobrain-check --root "<root>" --json` | 读操作，检查 deterministic exobrain 状态。 |
| 检查网关 | `python -B -m kb gateway-check --root "<root>" --json` | 检查本地 policy gateway readiness。 |
| 检查锁 | `python -B -m kb lock-check --root "<root>" --json` | 查看写锁状态。 |
| 恢复锁 | `python -B -m kb recover-lock --root "<root>" --manual-confirm --json` | 只在确认 stale 或 uncertain lock 后使用。 |
| 评估检索 | `python -B -m kb eval-search --root "<root>" --benchmark "meta/evals/retrieval-benchmark.jsonl" --json` | 真实检索 benchmark 需要明确范围。 |

## 选择规则

- 新用户先读 [中文快速开始](quickstart-zh.md)，再跑 [首次运行演示](first-run-demo.md)。
- 不确定本地状态时，先运行 `doctor`、`schema-check`、`product-console`，或用 [本地网页控制台](local-web-console.md) 查看只读浏览器视图。
- 任何草稿进入稳定 wiki 前，都必须经过 `validate-draft` 和 `publish-draft`。
- 任何 provider 相关动作前，先运行 offline `llm-preflight`，并确认当前任务允许 provider 使用。
- 任何备份、恢复、发布、候选记忆、benchmark 或真实库操作前，都要确认根目录和写入范围。

## Demo story command

`tools/run-demo.ps1` is the local expanded synthetic story runner. It reads public source fixtures from `examples/demo-story`, creates a temp synthetic root, runs `capture-candidate`, `review-candidate`, `publish-memory`, deterministic `validate-draft` and `publish-draft`, `trust-report`, `govern`, `backup`, `restore`, and `migrate-check`, then writes a redacted `synthetic-demo-story-v1` report. The expected demo boundary is `tracked_fixtures_mutated=false`.

## Retrieval quality benchmark report

`eval-search --json` returns a redacted `benchmark_report` with mode hit rates, quote-support metrics, duplicate warnings, stale-index hints, low-quality-source markers, privacy summary, and residual risks. `benchmark-add` may include `--expected-quote` for synthetic/local metric scoring. Expected quotes and retrieval hits are measurement aids only; they do not replace exact local quote evidence, source review, `validate-draft`, or `publish-draft`.
