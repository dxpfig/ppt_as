# Kimi 页面生成改造交接记录

记录日期：2026-10-22

状态：实现基本落地，尚未完成端到端验收。

## 2026-10-23 更新

- 产品决策改为：PPT 页面生成阶段保持 thinking 开启。
- `.env` 已设置 `PPT_KIMI_PAGE_REASONING_MODE=adaptive`。
- 代码默认值已从 `direct` 改为 `adaptive`，避免其他启动环境重新关闭 thinking。
- `.env.example` 已补充页面 reasoning 配置说明。
- 已验证页面请求控制为 `thinking: adaptive`、`reasoning_split: true`。
- 项目已通过 `pnpm dev` 启动，页面 thinking 配置已生效。
- 开发启动已改为显式监听 `0.0.0.0`，局域网入口为 `http://192.168.0.10:5173/`。
- P1 阻塞问题仍未修复；启动成功不代表全新 PPT 任务可以完成。

## 当前代码基线

- 当前工作树：`/home/figodxp/workspace/ppt_as`
- 当前分支：`kimi`
- HEAD：`20685b2578d3`（`perf: copy only referenced media for page previews`）
- 本地分支比 `origin/kimi` 领先 8 个提交。
- 当前仍有大量未提交改动；本轮审查针对磁盘上的最新工作树，不是正在运行的旧进程状态。
- `feature/kimi-slides-port` 仍未合入；其模板规范、surgical、设计系统等关键模块不在当前 `kimi` 树中。
- `08b8` 和 `10a3` 两个额外工作树仍有未提交内容，不能按“已全部合入”处理。
- `stash@{0}` 仍在，不能直接清理。

## 五项改造完成度

| 要求                                                              | 当前实现                                                                                                                             | 判断                                                                                                                  |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| 页面阶段使用独立 reasoning 配置                                   | 创建了独立 `page_model`；主模型和页面模型均使用 `adaptive`，OpenAI 兼容请求中 `thinking` 为 `adaptive`                               | 已完成页面 thinking 开关。当前没有单独的数值化 reasoning token/effort 上限；Anthropic 路径仍会忽略页面 reasoning 模式 |
| 页面 Agent 使用专用短提示词                                       | 页面 Agent 使用约 1464 字符的专用提示词，`allowed_tools=()`，不再加载完整 Kimi SKILL                                                 | 已实现                                                                                                                |
| 模型输出声明式 JSON，宿主负责坐标和 PPTD YAML，并保留自由布局通道 | 页面模型返回 JSON；`layout_engine.compile_page()` 计算坐标；宿主通过 `yaml.safe_dump()` 写入 PPTD；`free` 节点接受 0–1 归一化 bounds | 已实现                                                                                                                |
| 布局自由度不设硬上限                                              | 编译器和校验器没有元素数量硬上限；手工验证 60 个子节点可编译为 61 个页面元素                                                         | 硬上限已取消，但提示词仍有“6–14 个元素、约 1200 字”的软约束，需要确认这是否符合产品口径                               |
| 页面生成与前页渲染、结构检查、视觉检视流水线化                    | `DraftPreviewPublisher` 在页面生成前启动，每 0.5 秒扫描；逐页执行结构校验、Chromium 渲染和视觉检视，默认并发数为 3                   | 已实现。手工时序验证中第 2 页生成时，第 1 页仍在渲染                                                                  |

关键代码位置：

- 页面模型创建：`agent_service/kimi_agent.py:1156`
- 页面 Agent 构造：`agent_service/kimi_agent.py:1434`
- 页面 JSON 编译：`agent_service/kimi_agent.py:1466`
- 专用页面提示词：`agent_service/kimi_prompts.py:30`
- 声明式布局编译器：`agent_service/layout_engine.py:414`
- 增量预览/检查流水线：`agent_service/kimi_preview.py:372`

## P1 阻塞问题

全新任务在批准计划后、真正生成第一页之前会失败。

调用顺序：

1. `kimi_plan.write_plan_deck()` 生成 `deck/deck.pptd`，其中 `pages` 已列出所有计划页面，但不会创建对应的 `.page` 文件。
2. `run_kimi()` 随后调用 `kimi_workspace.load_deck(source_entry)`。
3. `load_deck()` 会立即读取 `deck.pptd` 中列出的每个页面文件。
4. 全新任务中这些页面尚不存在，因此抛出 `FileNotFoundError`。

相关位置：

- 计划只写页面清单：`agent_service/kimi_plan.py:439`
- 页面阶段提前加载完整 deck：`agent_service/kimi_agent.py:1387`
- `load_deck()` 读取每个页面：`agent_service/kimi_workspace.py:417`

复现测试：

- `agent_service.test_kimi_agent.KimiAgentUnitTests.test_run_kimi_plans_once_then_writes_pages_in_batches`
- `agent_service.test_kimi_agent.KimiAgentUnitTests.test_run_kimi_repairs_layout_issue_before_commit`

这两个测试都在页面写入前以 `pages/1.page` 不存在失败，说明问题在主流程，不是测试环境偶发。

## 自动化验证结果

已执行：

```bash
.venv/bin/python -m unittest \
  agent_service.test_kimi_agent \
  agent_service.test_kimi_preview \
  agent_service.test_generation_events
```

结果：

- 共运行 68 项测试。
- 4 项失败、2 项错误、1 项跳过。
- 2 项错误对应上面的 P1 主流程阻塞。
- 4 项失败来自 reasoning 测试仍断言旧策略；当前 `.env` 的 `PPT_LLM_REASONING_MODE=adaptive` 也会覆盖测试未显式清理的环境。
- `.venv/bin/python -m unittest agent_service.test_kimi_preview`：3 项全部通过。
- `py_compile`：`layout_engine.py`、`kimi_agent.py`、`kimi_preview.py`、`kimi_prompts.py`、`agents.py` 均通过。
- 当前虚拟环境未安装 `pytest`，应继续使用标准库 `unittest` 或项目既有 `pnpm` 测试命令。

额外手工验证：

- 自由布局：声明 60 个子节点，成功编译为 61 个元素，证明宿主没有元素数量硬上限。
- 流水线：模拟第 1 页渲染期间写入第 2 页，生成与预览检查确实发生重叠。

## 当前运行状态

以下是 2026-10-23 08:53 左右切换到局域网监听后的运行状态：

| 服务         |    PID | 端口 |
| ------------ | -----: | ---: |
| Python Agent | 245097 | 8001 |
| Express BFF  | 245692 | 3002 |
| Vite 前端    | 245582 | 5173 |

三个进程都从 `/home/figodxp/workspace/ppt_as` 的 `kimi` 分支启动，并监听 `0.0.0.0`。Python Agent 健康检查返回正常，模型为 `MiniMax-M3.1-Flash`，页面 thinking 已按 `adaptive` 启动。

局域网入口：

- 前端：`http://192.168.0.10:5173/`
- BFF：`http://192.168.0.10:3002/api/health`
- Agent：`http://192.168.0.10:8001/api/studio/health`

服务器本机通过上述 IP 地址检查时均返回 HTTP 200。UFW 当前处于启用状态，但普通用户无权读取规则；若其他局域网设备仍无法连接，需要在 UFW 中放行 5173、3002 和 8001 端口。

## 后续建议顺序

1. 先修复 P1：页面阶段不要要求尚未生成的页面全部可读，或在计划落盘时创建不会被视为已完成页面的占位文件。
2. 更新页面批处理集成测试，使其走新的 JSON 页面 Agent，而不是继续让假 Agent 写 YAML。
3. 页面 thinking 已确定保持开启，本轮不需要再验低 reasoning 配置；若以后需要控制成本，再增加独立页面 reasoning effort/token 配置。
4. 决定提示词中的“6–14 个元素、约 1200 字”是否保留。它不是运行时硬限制，但仍会约束模型输出。
5. 重新运行相关 `unittest`，确认不再有 P1 错误。
6. 重启 Python Agent，使最新代码进入运行态。
7. 使用真实模型至少跑一次全新 5 页任务，检查页面 JSON、自由布局、逐页结构/渲染/视觉检视和最终 PPTD/PPTX/PDF 交付。

## 不要立即清理的内容

- 不要删除 `stash@{0}`。
- 不要删除 `08b8` 或 `10a3` 工作树。
- 不要删除 `feature/kimi-slides-port` 分支；它包含当前 `kimi` 树中不存在的模板重建、surgical 和设计系统代码。
