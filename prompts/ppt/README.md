# PPT 提示词库

此目录是 PPT Agent 的提示词唯一来源。每次生成任务重新读取，无需重启；同一任务使用同一份快照。修改后下一次生成生效，已有演示不会被自动改写。

参考用户提供的 `kimi-slides/SKILL.md`、`reference/slides_categories.md` 及各场景指南，针对本项目 AgentScope + 原生可编辑 PPTX 流程整理。未依赖 Kimi 专用 CLI、PPTD 编辑器或 KIMI_REF。

| 文件 | 职责 |
| --- | --- |
| common.md | 证据边界、表达、页数、资料处理 |
| layouts.md | 已实现的版式与图片策略、文字预算 |
| planner.md | 受众任务、叙事顺序、逐页设计计划 |
| writer.md | 按设计计划撰写可排版的页面 |
| editor.md | 局部编辑、事实和未涉及内容的保留 |
| repair.md | 有限纠正密度或结构问题 |
| scenarios/index.md | 场景分类 |
| scenarios/*.md | 各场景的叙事与证据重点 |

加载器：`agent_service/prompt_library.py`。目录中文件缺失或为空会明确报错，避免静默退回旧提示词。每次生成记录内容哈希 `generationMeta.promptVersion`，可复现本次采用的规则版本。提示词描述的字段和版式必须与 `agent_service/models.py`、`agent_service/design.py` 一致。

编辑建议：先改对应角色/场景文件，使用新的临时工作记录生成，检查实际 PPT/PDF，再应用到正式内容。不要在提示词中放 API Key 或生产密码。提示词只能改变策划与文案；新增图表类型或任意布局需要同时实现渲染器。
