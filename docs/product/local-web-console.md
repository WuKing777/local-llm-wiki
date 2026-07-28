# 本地网页控制台

本地网页控制台是 `product-console` 的浏览器视图。它只绑定本机 loopback，默认地址是 `127.0.0.1`，用于查看健康状态、依赖 warning、profile 状态、治理状态、备份状态、Obsidian 打开描述和可复制命令。

启动命令：

```powershell
python -B -m kb web-console --root "<root>" --no-open
```

测试或脚本可以使用临时端口，并且不自动打开浏览器：

```powershell
python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open
```

## 安全边界

- 本地网页控制台默认只监听 `127.0.0.1`，不推荐也不默认提供 LAN、远程或托管服务。
- 浏览器路由是只读的，不执行 backup、restore、publish、ingest、llm-draft、provider preflight 或其它会改变知识库状态的动作。
- 页面展示的是 redacted product state 和 copyable command descriptors；复制命令后仍需要你在终端里确认并运行。
- Cloud or LLM use is off by default。控制台不会调用真实 DeepSeek、真实云 LLM、真实 embedding provider 或外部服务。
- AI/LLM output is never a fact source。稳定 wiki 内容仍必须经过 `validate-draft` 和 `publish-draft`，并使用本地 quote evidence。
- 不要把示例命令指向真实用户库或私人资料根目录，除非后续 exact-path 任务明确批准。

## 页面包含什么

- Root health：来自本地 `doctor` 的 redacted 状态摘要。
- Dependency warnings：LLM、embedding、OCR 的本地配置状态。
- Profile status：本地 profile registry 的 redacted 计数和选中状态。
- Governance status：lint、status、governance 的描述性摘要。
- Backup status：备份 freshness 的描述性摘要。
- Obsidian descriptor：只显示打开描述，不从浏览器执行写入。
- Available actions：显示 `product-console` 的 action descriptors，并给出可复制命令。
- Safety notices：本地优先、无默认 provider、无真实库操作、证据门禁和 no-write 浏览器边界。

## 与命令行的关系

本地网页控制台不是完整 GUI、安装器、Obsidian plugin、远程服务或 trust-report 执行器。它可以显示可复制的 `trust-report` 命令描述，但不会从浏览器执行报告、发布、修复或其它写操作，也不会绕过 PolicyGateway、source review、write locks、redaction、validate/publish gates、privacy checks 或 public export boundary。

需要机器可读状态时继续使用：

```powershell
python -B -m kb product-console --root "<root>" --json
```

需要选择下一步命令时，读 [命令选择指南](command-guide.md)。
