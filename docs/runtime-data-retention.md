# 运行数据保留与清理

`STUDIO_DATA_DIR` 保存用户项目和交付内容。默认目录为 `runtime/studio/`，已被 Git 忽略。

## 权威数据

以下目录不可由自动清理工具删除：

- `projects/`：项目元数据和活动任务引用。
- `documents/`、`document_versions/`：权威 PPTD 当前指针与不可变版本。
- `templates/`、`template_references/`：公司模板及导入参考。
- `reference-files/`：用户参考资料原件和解析正文。
- `artifacts/`：已发布 PPTX、PDF、预览和导出报告。
- `jobs/`、`requests/`、`checkpoints/`：任务、恢复请求和检查点。

这些目录应按站点备份策略保留。删除项目时优先使用产品删除接口，内容会进入 `trash/` 并提供撤销。

## 可重建缓存

- `render-cache/`：页面渲染结果。
- `review-cache/`：视觉审稿结果。
- `export-cache/`：交付产物缓存。

缓存不参与 revision 权威判断，可删除；下一次任务会重建。默认清理工具只处理这些目录和超期 `.tmp` 文件。

## 候选稿

`visual_candidates/` 保存视觉审稿和页面设计的私有候选稿。它们可能用于故障恢复，因此默认不清理。确认不需要恢复时，可显式使用 `--include-candidates`。

## 清理命令

默认只预览：

```bash
.venv/bin/python scripts/cleanup_runtime.py --runtime runtime --days 7
```

执行安全缓存清理：

```bash
.venv/bin/python scripts/cleanup_runtime.py --runtime runtime --days 7 --apply
```

包含超期候选稿：

```bash
.venv/bin/python scripts/cleanup_runtime.py \
  --runtime runtime --days 14 --include-candidates --apply
```

工具会拒绝清理文件系统根目录，并拒绝任何解析后逃逸 `runtime` 的目标。

## 备份要求

升级、回滚、分区维护或迁移主机前，至少备份全部权威数据目录。`render-cache/`、`review-cache/`、`export-cache/` 可不备份。
