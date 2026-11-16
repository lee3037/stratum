# 开发

[← 返回 README](../README.md)

## 目录结构

```
app/
  parsers/      解析后端注册表、公共契约和本地方案
  extract.py    工作进程：版面分析、OCR、表格框线识别 → 页面结构
  structure.py  页眉页脚清理、标题分级、段落/列表重建、跨页合并
  tables.py     表格清洗、跨页合并、Markdown/键值渲染
  chunker.py    章节分块、上下文、分隔符拼接
  pipeline.py   选择解析后端、汇总警告、调用分块器
  storage.py    服务端文件库：知识库目录、路径校验、上传去重、版本分配
  scheduler.py  解析进程池调度：单个解析优先、同优先级轮转、进程崩溃后重建
  batches.py    批量解析：批次持久化、跨批次轮转、重启恢复
  exports.py    打包下载：zip 与汇总.csv
  fastgpt.py    FastGPT 对接：知识库绑定、导入记录、导入与替换
  server.py     FastAPI 服务、知识库接口与任务管理
deploy/         systemd 服务与配置示例
web/            前端源码（React + Tailwind + shadcn）
static/         前端构建产物（已提交）
docs/           文档；docs/assets/ 保存 README 海报及其 HTML 源文件
samples/        示例 PDF 和转换结果
tests/
  make_samples.py   生成测试 PDF（文本型、复杂表格、双栏、扫描型）
  run_sample.py     命令行转换
  test_pipeline.py  转换流水线测试
  test_api.py       接口测试
  test_storage.py   文件库测试
  test_library_api.py 知识库接口测试
  test_batches.py   批量解析与打包下载测试
  test_fastgpt.py   FastGPT 导入测试（内存中的假 FastGPT）
  test_scheduler.py 进程池调度测试：优先级、轮转、撤下剩余分组、崩溃恢复
  test_multiuser.py 多人并发测试：修改冲突、设置并发写入、任务上限、磁盘保护
  fake_fastgpt.py   本地假 FastGPT 服务，供浏览器冒烟测试使用
  ui_smoke.py       浏览器冒烟测试（需另装 playwright；STRATUM_DATA_DIR 与服务保持一致）
```

## 扩展解析方案

解析后端遵循 `app/parsers/base.py` 中的 `DocumentParser` 契约。`parse()` 返回 `ParsedDocument`，包含结构化元素、文档统计和解析警告。统一流水线把元素交给 `chunker`，前端无需处理后端原始响应。

1. 在 `app/parsers/` 中实现后端。
2. 在注册表中调用 `register()`。
3. 验证页码、表格结构和重新分块结果。

`id` 是请求中的 `parser` 值，`name` 是界面名称。`local_ocr_options` 控制本地 OCR 参数的显示。`uses_local_workers` 决定任务是否需要本地解析进程池。

`GET /api/parsers` 从注册表生成方案目录。前端读取这个目录，显示可用方案。未传 `parser` 的旧请求仍使用 `pymupdf`。未知方案返回错误。

## 并发模型

服务以单个 uvicorn 进程运行，内部分三层：

1. **请求线程**（FastAPI 线程池）：处理上传、保存、导入等请求。文件写入经过 `storage` 中的锁和「先写临时文件再发布」，不会互相覆盖。
2. **任务线程**：单个解析每个任务一个线程（`_runner`，最多 `STRATUM_MAX_JOBS` 个）；批量解析由 `BatchManager` 的 `STRATUM_BATCH_CONCURRENCY` 个工作线程在批次之间轮流取文件。
3. **解析进程池**：`pipeline.convert` 把 PDF 切成页面分组提交给执行器。执行器是 `scheduler.PriorityPool.session(priority)` 返回的视图：分组先进入本地优先级队列 `(优先级, 文件内序号, 提交顺序)`，调度线程只在有空闲进程时才提交给 `ProcessPoolExecutor`。因此单个解析（`INTERACTIVE`）总能插到批量（`BATCH`）前面，同优先级的文件按序号交替执行。文件解析结束时，`session` 撤下它尚未执行的分组。

注意：`ProcessPoolExecutor` 在持有内部锁时执行完成回调，所以调度线程调用 `submit()` 时不能持有调度器自己的锁，否则可能死锁。进程池失效（`BrokenProcessPool`）时调度器丢弃旧池、按需重建，受影响的分组重试一次。

## 测试

```bash
.venv/bin/python -m pytest tests -q                  # 全部测试
.venv/bin/python -m pytest tests -q -m "not slow"    # 跳过扫描件用例（这些用例较慢）
.venv/bin/python tests/run_sample.py a.pdf max_chars=800 table_mode=kv   # 命令行转换单个文件
```

浏览器冒烟测试的用法见 `tests/ui_smoke.py` 开头的说明：先启动假 FastGPT 和服务，再运行脚本。每次运行前重启假 FastGPT，否则上次留下的集合会让导入被当作冲突跳过。

## 前端

前端组件通过 shadcn CLI 从 cult-ui 和 Spectrum UI 注册表引入。注册表配置在 `web/components.json` 中。组件源码保存在 `web/src/components/`，可以直接修改。

```bash
cd web
pnpm install
pnpm dev            # 开发服务器，/api 代理到 http://localhost:8765（可用 STRATUM_API 指定）
pnpm build          # 构建到 ../static
pnpm dlx shadcn@latest add @cult-ui/<组件名>   # 引入新的 cult-ui 组件
```

> [!NOTE]
> 修改前端后，执行 `pnpm build` 并提交 `static/` 目录。

## 品牌素材

- logo：`web/public/favicon.svg` 和 `web/src/components/logo.tsx` 是同一图形。修改时两处保持一致。
- 海报：源文件是 `docs/assets/poster.html`。文件头部注释写了重新生成 `poster.png` 的命令。
