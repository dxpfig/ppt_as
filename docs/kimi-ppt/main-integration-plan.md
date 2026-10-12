# PPTD 主应用闭环

本轮执行已由用户确认。PPTD v2 文件是唯一内容权威；项目 JSON 只保存对话、任务、偏好和产物引用。保留旧项目，使用明确迁移入口。原文规则档案保持不变。

## 分工与接口

- 生成：`pptd_generation.py`、`kimi_rules.py`、`pptd_validation.py`，注入完整原文和对应场景规则，AgentScope 规划、素材检索、逐页生成、检查点和多轮修复。用户未指定时自动页数。结构通过不能等同渲染通过。
- 存储及引擎接入：`pptd_store.py`、`pptd_exports.py`、`pptd_engine/workbench.py`，不可变版本、原子发布、乐观锁、版本恢复、同一快照导出 PPTX/PDF/源包。
- 前端：`PptdStudio`、`usePptdStudio`，主页面实际 PPTD 工作台、同源 iframe 页面选中/文档变更桥、版本和任务恢复。
- 服务协调：持久化原始请求、检查点和事件，SSE、取消、失败重试、重启后显式恢复；不会自动重复付费生成。内容编辑与旧 autosave 分离，过期任务不能覆盖新版本。

文档 API 前缀 `/api/studio/projects/{id}/pptd`：GET 主数据、`/view`、`/document`、`/fonts`、`/capabilities`、`/assets/{path}`、`/source.zip`、`/versions`；POST `/edit`、`/migrate`、`/versions/{versionId}/restore`。写入必须携带 `expectedRevision`。

`project.workflow=pptd-v2` 表示格式；`job.workflow=new|edit|recreate` 表示工作模式。任务 `/events` 发送完整状态，`/retry` 从头重试，`/resume` 从有效检查点续接，均创建新的任务记录保留原失败记录。

## 验收顺序

1. 持久化、重启、取消、版本冲突、源字段保留行为测试。
2. 浏览器实际排版、PNG、PDF 文本/公式与 PPTX 包结构验证。
3. 前后端构建与回归测试。
4. 已配置 AgentScope 模型实际生成两页，主 UI 预览和元素修改，再导出检查一致性。
5. 独立代码审阅，记录真实限制；不声称完成不可用的 Microsoft PowerPoint GUI 验收。
