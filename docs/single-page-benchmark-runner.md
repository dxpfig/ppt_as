# 单页模板基准 Runner

固定用例、预算和输入分别来自：

- `benchmarks/single-page-template/cases.v1.json`
- `benchmarks/single-page-template/inputs.v1.json`
- `docs/single-page-template-test-cases.md`

## 运行单个用例

开发诊断先逐个用例执行 smoke：

```bash
pnpm bench:single-page --case COV-01 --mode smoke
```

复现本次高密度第 2 页失败场景：

```bash
pnpm bench:single-page --case REG-P02 --mode smoke
```

不调用模型，只校验用例、模板和配置：

```bash
pnpm bench:single-page --case COV-01 --mode smoke --dry-run
pnpm bench:single-page --case REG-P02 --mode smoke --dry-run
```

只有明确需要正式门禁时才运行全部用例。smoke 每例 1 次，formal 每例 3 次：

```bash
pnpm bench:single-page --mode formal
```

可用重复的 `--case` 或逗号分隔值选择多个用例。

全量运行默认只执行 10 个强制用例。`REG-P02` 是补充诊断回归用例，标记为
`includeInFullSuite=false`，必须显式选择才会执行。

## 隔离和固定条件

每次调用创建新的结果目录并启动独立 Agent 进程，不复用当前开发服务的任务、
模板或缓存。启动前强制校验：

- 固定模板路径与 SHA-256。
- 10 个强制用例和对应输入夹具；补充诊断用例必须与输入夹具保持 ID 一致。
- 主模型与视觉模型均为 `MiniMax-M3.1-Flash`。
- 浏览器渲染器可用。
- `PPT_RENDER_CACHE=0`、`PPT_REVIEW_CACHE=0` 和 benchmark trace 已启用。

每个用例使用独立项目 ID。模型和缓存条件不符合时 runner 直接失败，不产生通过
记录。

## 产物

每次运行写入独立目录：

```text
runtime/benchmarks/single-page-template/<日期>/<会话>/
  environment.json
  agent.log
  COV-01/
    run-1/
      request.json
      job.json
      project.json
      document.json
      events.jsonl
      poll.jsonl
      timing-trace.json
      draft-preview.json
      run.json
      run-1.page
      run-1.png
      artifacts/
  summary.json
  summary.md
```

目录使用时间戳和随机令牌，runner 从不覆盖历史结果。

## 判定

`run.json` 同时保留任务状态和首轮基准结论。首轮通过要求：

- 任务完成且严格生成 1 页。
- 没有 `retrying` 事件或修复模型调用。
- 首轮及最终的重叠、裁切、溢出、对比度和视觉问题均为 0。
- AI 视觉审稿为 `passed`，引用的元素 ID 全部真实存在。
- 必需文字和数字完整，标题只出现一次，禁止保留的模板原文字未出现。
- 编译元素数不超过用例硬上限。
- 总耗时不超过用例首轮硬时限。

修复后通过仅保留为诊断信息，始终记为 `firstPass=false`。`timing-trace.json`
记录模板加载、规划、页面模型、编译、结构校验、模板叠加、浏览器渲染、几何
测量、视觉审稿和修复的每次调用；汇总报告给出每个用例的中位数、最大值和主要
耗时阶段。
