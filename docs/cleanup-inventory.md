# 代码与目录清理清单

最后更新：2026-10-21

清理原则：先确认引用和测试影响，再归档或删除；运行数据和用户参考文件不做不可恢复删除。

## 保留

| 路径                      | 原因                                               |
| ------------------------- | -------------------------------------------------- |
| `agent_service/`          | FastAPI、生成、审稿、修复、页面操作和导出          |
| `backend/`                | Express BFF                                        |
| `frontend/`               | PPT Studio 工作台                                  |
| `pptd_engine/`            | PPTD 导入、校验、渲染和原生 PPTX 导出              |
| `packages/pptd-runtime/`  | 浏览器 PPTD 运行时                                 |
| `deploy/`                 | 源码、Docker 和离线部署脚本                        |
| `scripts/`                | 服务管理、模型检查、性能报告和 lint 工具           |
| `tests/golden/`           | 重构黄金场景清单                                   |
| `experiments/pptd-v2/`    | 被 PPTD 引擎测试和开发脚本直接引用，不能删除       |
| `skills/pptx-surgical/`   | 模板外科编辑能力来源和脚本                         |
| `docs/kimi-ppt/original/` | 原始规则和契约，运行时读取，必须保持内容与哈希不变 |

## 归档候选

| 路径                                                        | 现状           | 建议                                        |
| ----------------------------------------------------------- | -------------- | ------------------------------------------- |
| `ppt_moduel/信息化项目汇报模板.pptx`                        | 已从工作树移除 | 恢复标签 `archive/pre-w11-cleanup-20261021` |
| `experiments/kimi_slide_research/Kimi做PPT功能逆向报告.rar` | 已从工作树移除 | 恢复标签 `archive/pre-w11-cleanup-20261021` |
| `experiments/kimi_slide_research/kimi-slides.rar`           | 已从工作树移除 | 恢复标签 `archive/pre-w11-cleanup-20261021` |

这些文件已创建恢复标签并从当前工作树移除。需要查阅历史资料时通过标签恢复，不再放入当前发布树。

## 已忽略的生成内容

- `runtime/`：项目、文档版本、任务、模板、产物和日志。
- `frontend/dist/`、`backend/dist/`、`packages/pptd-runtime/build/`：可重新构建。
- `.remember/`、`.traces/`：工具运行记录。
- `experiments/long_file_ppt_ex/*.pptx`：生成的质量对比文件。
- `experiments/long_file_ppt_ex/analysis/*.png`：质量对比截图。

## 已完成的低风险清理

- 删除项目内的 `__pycache__` 目录。
- 为长文档实验 PPTX 和截图增加 `.gitignore`。
- 保留 `runtime/` 数据，等待单独的数据保留策略。
- 新增 `docs/runtime-data-retention.md` 和干跑优先的 `scripts/cleanup_runtime.py`。
- 删除可重建 Python 缓存和旧前端/后端构建产物；依赖目录与权威运行数据保留。

## 后续删除门槛

1. 使用 `rg` 确认无代码引用。
2. 使用 `git log` 和文档确认无历史交付价值。
3. 创建归档标签或外部备份。
4. 删除后运行 `pnpm test:fast`、`pnpm build` 和核心合同测试。

## 运行数据

运行时清理遵循 [运行数据保留与清理](runtime-data-retention.md)。默认工具不会删除项目、文档版本、模板、参考资料、任务、检查点或交付产物。
