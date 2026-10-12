# 动态模型上下文适配实施计划

> 使用 executing-plans 在当前会话执行，test-driven-development 验证失败与修复。用户已批准上一轮的五项方案并要求执行。

**Goal:** 保留完整 Kimi 规则和所有验收门禁，根据当前服务公布的窗口管理长稿生成，避免无界历史与静默截断。

**Latest steering:** 用户明确要求先请求接入模型的上下文限制，不为 Qwen 写死 256K。OpenAI 兼容接口和 Anthropic 原生接口分别发现能力；缺字段显式标记配置/回退预算。

**Architecture:** 模型适配层对真正的模型输入计数并阻止超限请求；生成层提供有界页面上下文与原文分块读取；检查点保存资料处理进度。vLLM 已开启前缀缓存，保持稳定提示词前缀。

**Tech Stack:** Python 3.12、AgentScope、OpenAI SDK、vLLM /tokenize、unittest。

**Spec:** 本会话已批准的方案：对齐上下文配置、预算检查、全稿摘要与必要完整页/按需原文、长资料分段、核查前缀缓存。

## Global Constraints

- 完整原始 Kimi 文件逐字保留；不更改页数、结构/渲染/AI 视觉验收。
- API 密钥仅在服务端；tokenizer 地址必须同源；原始错误响应不进入 UI。
- 输入加输出预留加安全余量必须小于等于有效服务窗口；实际 tokenizer 不可用时明确失败，不猜测精确值。
- 无 Git 仓库，直接在已备份的当前工作区执行，不创建虚假提交。
- 当前生成任务继续运行；部署时通过现有取消/恢复接口保存并恢复任务，不丢弃已完成页面。

## Review Focus

1. 修复重试累积消息、图片和工具调用导致超限。
2. tokenizer 下线或报告更小窗口不能放行超限请求。
3. 并行页完成顺序不能改变历史窗口、泄露后续页面或修改原稿。
4. 原文切块须无遗漏、无改写，越界/未知引用不能读取其他文件。
5. 资料处理失败恢复要跳过已完成块，当前请求和设计规范不能被摘要取代。

## Task 1: 模型预算与诊断

Files: agent_service/model_context.py、agents.py、job_execution.py、test_model_context.py。

- [x] 写 HTTP transport 测试：总预算边界、超限不发 completion、服务上限更小、tokenizer 故障、格式重试；例如输入 900 + 输出 100 + 余量 25 在窗口 1024 时必须拒绝。
- [x] 运行新测试，确认失败；实现同源精确计数、上下文配置与安全诊断，运行测试至通过。
- [x] AgentScope 自动压缩入口也遵守预算；防止先摘要再掩盖超限。

## Task 2: 有界生成上下文与资料处理

Files: agent_service/generation_context.py、pptd_generation.py、kimi_rules.py、test_generation_context.py。

- [x] 测试 50 页上下文只带最近两页原始 JSON、完整规划保留、原页按引用读取且不修改输入；测试乱序完成。
- [x] 测试分块拼接等于原文、引用只读、摘录必须是原文、资料检查点恢复。
- [x] RED 后实现页面目录、原文只读请求、长资料分块摘录与检查点；格式重试从完整请求重新开始，避免无界堆积。
- [x] 跑针对测试与既有生成/修复测试，保持门禁不变。

## Task 3: 部署与验证

Files: .env、.env.example、docs/local-models.md、package.json。

- [x] 启用 auto 能力发现，按窗口动态调整输出/余量并自动识别 vLLM /tokenize；将新测试加入 test:studio。
- [x] 运行 pnpm test:studio，检查所有结果；独立代码审查并修复重要问题。
- [x] 用真实 tokenizer 比较历史上下文优化前后；用实际模型验证读取原文及 JSON 输出。
- [x] 保存活动任务检查点，重启服务并恢复；检查新请求正常生成、已完成页未丢失。
- [x] 记录缓存实际设置与验证数据，不将 token 降幅表述为实测整稿速度提升。
