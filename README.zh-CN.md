# Local LLM Wiki

本页是公开快照的中文入口。English entry: [English README](README.md)。

Local LLM Wiki 是本地优先、文件优先的个人知识库工具，用于管理本地来源材料、wiki 页面和可重建的搜索元数据。稳定内容保存在普通文件中；SQLite、向量索引、缓存和报告属于可重建的本地生成状态。

## 公开克隆与运行

从干净公开快照开始，需要 Python 3.11+ 和 Git：

```powershell
git clone "https://github.com/WuKing777/local-llm-wiki.git" "local-llm-wiki"
cd "local-llm-wiki"
python -B -m pip install -e .
python -B -m kb --help
kb --help
python -B -m kb doctor --root "examples/demo-root"
python -B -m kb product-console --root "examples/demo-root" --json
python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open
```

仓库 URL 占位符只用于私有源模板。发布导出会将它替换为已批准的 GitHub 克隆地址；最终公开产物不得残留仓库 URL 占位符。

一条命令的演示路径仅使用 synthetic demo 数据：

```powershell
.\tools\run-demo.ps1
```

建议先阅读 [中文快速开始](docs/product/quickstart-zh.md)，再运行 [首次运行演示](docs/product/first-run-demo.md)。产品文档还包括 [本地网页控制台](docs/product/local-web-console.md)、[命令选择指南](docs/product/command-guide.md)、[Installation](docs/product/installation.md) 和 [Open Source Release](docs/product/open-source-release.md)。

`examples/demo-root` 是公开的合成演示根。不要把演示命令指向真实用户库、私有来源集合、真实 provider 或生产根。创建新的本地根时，只使用你控制的目录，并显式传入 `--root`：

```powershell
python -B -m kb init --root "<root>"
python -B -m kb doctor --root "<root>"
python -B -m kb schema-check --root "<root>" --json
python -B -m kb product-console --root "<root>" --json
python -B -m kb web-console --root "<root>" --no-open
```

## 安全边界

本项目本地优先：命令只操作显式传入的本地根，公开 smoke 命令只使用 synthetic demo 数据。真实用户库数据不会上传。默认不会配置或调用云端/LLM provider。Cloud or LLM use is off by default. No cloud or LLM provider is configured or called by default.

LLM 输出永远不是事实来源。LLM 可以在批准的本地上下文上组织、总结、推理和起草，但稳定知识必须有本地证据，并通过 `validate-draft` 与 `publish-draft` 门禁；稳定 wiki 内容需要本地精确引用证据。Local bge-m3 embeddings 只是检索加速器，不是推理权威或引用权威。

不要保存或提交 API keys、bearer tokens、prompts、完整 provider responses、private source text、raw chunks、真实用户库产物或具体私有路径。公开发布只能使用 clean snapshot/new public repository/squash import 或等价的历史隔离边界，不能发布 private Git history。

## 常用链接

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [Roadmap](docs/product/roadmap.md)
- [Release Checklist](docs/product/release-checklist.md)
- [Installation](docs/product/installation.md)
- [Open Source Release](docs/product/open-source-release.md)
- [Privacy and Secrets](docs/product/privacy-and-secrets.md)
- [Provider Preflight](docs/product/provider-preflight.md)
- [Examples](examples/README.md)

贡献、问题反馈和功能建议都必须使用合成或最小化信息，不能包含秘密、私有原文、raw chunks、prompts、完整 provider responses、具体私有路径、真实用户库产物或发布声称。
