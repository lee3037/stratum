# 部署

[← 返回 README](../README.md)

## 在 Ubuntu 服务器上安装

Stratum 只用 CPU，不占显存，可以和大模型、FastGPT 部署在同一台服务器上。以下以 `/opt/stratum` 为程序目录、`/data/stratum` 为数据目录。

1. 准备用户和目录：

   ```bash
   sudo useradd --system --create-home --shell /usr/sbin/nologin stratum
   sudo mkdir -p /opt/stratum /data/stratum/library /data/stratum/work
   sudo cp -r . /opt/stratum            # 在项目目录中执行
   sudo chown -R stratum:stratum /opt/stratum /data/stratum
   ```

2. 安装依赖。需要 Python 3.12（没有时可用 `conda create -n stratum python=3.12` 或 uv）：

   ```bash
   cd /opt/stratum
   sudo -u stratum python3.12 -m venv .venv
   sudo -u stratum .venv/bin/pip install -r requirements.txt
   ```

   服务器不能访问外网时，使用内网 PyPI 镜像（`pip install -i <镜像地址> ...`），或在能联网的同架构机器上执行 `pip download -r requirements.txt -d wheels`，把 `wheels` 目录拷到服务器后执行 `pip install --no-index --find-links wheels -r requirements.txt`。OCR 模型随 `rapidocr_onnxruntime` 安装，运行时不联网。

3. 配置并启动：

   ```bash
   sudo mkdir -p /etc/stratum
   sudo cp deploy/stratum.env.example /etc/stratum/stratum.env   # 按需修改
   sudo cp deploy/stratum.service /etc/systemd/system/
   sudo systemctl daemon-reload && sudo systemctl enable --now stratum
   curl http://127.0.0.1:8000/api/health
   ```

4. 在浏览器中打开 `http://<服务器地址>:8000`。防火墙需要放行该端口（例如 `sudo ufw allow 8000/tcp`）。

升级：把新代码复制到 `/opt/stratum`（不要覆盖 `.venv`），依赖有变化时重新执行 `pip install -r requirements.txt`，然后 `sudo systemctl restart stratum`。重启时正在进行的批量解析会在启动后自动继续；正在进行的单个解析会丢失，需要重新点击解析。

> [!IMPORTANT]
> 服务必须以**单个进程**运行（不要给 uvicorn 加 `--workers`，也不要在同一个数据目录上启动多个实例）。并发保护（版本号分配、修改冲突检测、批量队列、FastGPT 导入）依赖进程内的锁和队列。解析本身由服务内部的进程池并行处理，单进程不影响速度。

## 环境变量

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址和端口 |
| `STRATUM_DATA_DIR` | 项目目录下的 `data/library` | 文件库根目录。每个子目录是一个知识库，PDF 和解析结果都保存在这里。 |
| `STRATUM_WORKERS` | 8 核及以下：CPU 核数 / 2，取 1 至 3；更多核：CPU 核数 / 4，取 3 至 16 | 解析进程数。每个进程占用 350 MB 内存。 |
| `STRATUM_BATCH_CONCURRENCY` | 解析进程数 / 6，取 1 至 4 | 同时批量解析的文件数。每个文件的页面仍由全部解析进程并行处理。 |
| `STRATUM_OCR_THREADS` | CPU 核数 / 进程数 | 每个解析进程的线程数，版面分析模型和 OCR 共用。解析进程数 × 线程数不要超过分给 Stratum 的核数，否则线程互相争抢反而更慢。 |
| `STRATUM_MAX_MB` | 200 | 上传文件大小上限，单位 MB |
| `STRATUM_MAX_PAGES` | 2000 | 页数上限 |
| `STRATUM_JOB_TTL` | 10800 | 任务结果和上传文件的保留时长，单位秒 |
| `STRATUM_WORK_DIR` | 系统临时目录 | 直接上传解析（`POST /api/jobs`）和打包下载的临时目录。服务停止时只删除其中的任务子目录。 |
| `STRATUM_MAX_JOBS` | 解析进程数 × 2，至少 8 | 同时进行的单个解析任务上限。超出时提示「请稍后再试」。批量解析有自己的队列，不受此限制。 |
| `STRATUM_MIN_FREE_MB` | 2048 | 磁盘剩余空间低于这个值时，拒绝上传、保存和打包，避免把磁盘写满。查看文件不受影响。 |

### 图片地址

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STRATUM_PUBLIC_URL` | 用户在浏览器中访问 Stratum 的地址 | 写入 Markdown 的图片链接前缀，例如 `http://192.162.10.41:18080`。必须是使用 FastGPT 的人的浏览器能打开的地址，不能写 `127.0.0.1`。 |

PDF 中的图片保存在 `STRATUM_DATA_DIR/.images/`，Markdown 中以绝对地址引用。FastGPT 显示引用内容时，图片由使用者的浏览器直接从这个地址加载，所以：

- 建议显式配置 `STRATUM_PUBLIC_URL`。不配置时系统使用最近一次从非本机访问 Stratum 的地址；经反向代理访问且代理没有传递端口时，这个地址可能不对。
- 链接在解析时写入。修改这个地址后，已保存、已导入的版本仍是旧地址，需要重新解析并导入。
- FastGPT 用 HTTPS 访问而 Stratum 是 HTTP 时，浏览器可能拦截图片。两者使用相同协议。
- 图片地址不需要登录即可访问（文件名是 40 位内容哈希，无法猜测），与服务整体一样只适合内网。

> [!CAUTION]
> 服务没有登录鉴权。只在内网部署服务，或把服务放在带鉴权的反向代理后面。

## FastGPT

导入 FastGPT 通过 FastGPT 的 OpenAPI 进行，地址和密钥只在服务端配置，浏览器拿不到。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STRATUM_FASTGPT_URL` | 无 | FastGPT 根地址，例如同机部署时的 `http://127.0.0.1:3000`。不配置时界面不提供导入。 |
| `STRATUM_FASTGPT_API_KEY` | 无 | FastGPT API 密钥，需要对所有要导入的知识库有读写权限。 |
| `STRATUM_FASTGPT_TIMEOUT` | `120` | 请求超时，单位秒。 |
| `STRATUM_FASTGPT_SPLIT_MODE` | `char` | 创建集合时的 `chunkSplitMode`。按检测脚本的建议设置。 |
| `STRATUM_FASTGPT_CHUNK_SIZE` | 最大字数 × 2，至少 1500 | 创建集合时的 `chunkSize`，只作为上限，实际分块由分隔符决定。 |
| `STRATUM_FASTGPT_INDEX_SIZE` | 不传，使用 FastGPT 默认值 | 创建集合时的 `indexSize`，必须小于向量模型的最大 token 数。 |

> [!NOTE]
> FastGPT 用 Docker 部署时，`127.0.0.1:3000` 指宿主机映射的端口。确认 `curl http://127.0.0.1:3000` 在 Stratum 所在机器上可以访问。

配置完成后，在服务器上运行检测脚本，确认连通性和分块参数：

```bash
.venv/bin/python deploy/fastgpt_probe.py                  # 连通性与可访问的知识库
.venv/bin/python deploy/fastgpt_probe.py --dataset <知识库ID> # 分块测试：创建并立即删除几个临时集合
```

脚本读取 `stratum.env` 或 `/etc/stratum/stratum.env`，输出中不包含 API 密钥。如果默认的 `char` 方式没有得到 3 块，按脚本建议设置 `STRATUM_FASTGPT_SPLIT_MODE`。

## 多人同时使用

所有人的解析共用同一组解析进程（`STRATUM_WORKERS`）。每个 PDF 被切成若干页面分组（每组最多 8 页），由调度器分配给空闲进程：

- **单个解析优先**：有人点「解析」时，他的页面分组排在所有批量分组前面。已经在执行的批量分组不会被打断，所以最多等一个分组完成（通常几秒到几十秒，扫描件较慢），批量解析随后继续。
- **同时解析的文件轮流执行**：几个人同时解析时，各文件的分组交替执行，每个人都能看到进度，不会一个大文件独占全部进程。批量解析也一样，各批次之间轮流取文件。
- **任务上限**：同时进行的单个解析超过 `STRATUM_MAX_JOBS` 时，新请求会提示稍后再试。
- **不重复处理**：已在某个批次中排队或解析的文件，再次加入新批次时直接跳过。同一个 PDF 同时只有一个 FastGPT 导入，后到的请求等前一个完成后显示「已导入」，不会在 FastGPT 中重复建集合。
- **修改冲突**：两人同时修改同一个 Markdown 版本时，先保存的一方成功，另一方提示「已被其他人修改」，不会互相覆盖。另存为新版本时，各自得到不同的版本号。
- **进程崩溃自动恢复**：解析进程意外退出（内存不足，或个别 PDF 触发底层错误）时，服务自动重建进程池，受影响的分组重试一次；仍然失败的文件显示错误，其他人的解析不受影响。

## 运维

**健康检查**：`GET /api/health` 返回服务状态，可接入监控：

| 字段 | 说明 |
| --- | --- |
| `pool.running` / `pool.slots` | 正在执行的页面分组数 / 解析进程数 |
| `pool.pending` | 排队中的页面分组数，分 `interactive`（单个解析）和 `batch`（批量） |
| `pool.waiting_files` | 排队中的文件数，同样分两类 |
| `pool.restarts` | 解析进程池因进程崩溃重建的次数。持续增长时检查内存（`MemoryMax`）和日志。 |
| `active_jobs` / `max_jobs` | 进行中的单个解析数 / 上限 |
| `batches` | 进行中的批次数、排队和正在解析的批量文件数 |
| `storage` | 文件库路径、是否可写、剩余空间（`free_mb`）、是否低于下限（`low_space`） |

**日志**：`journalctl -u stratum -f`。解析失败、批量任务异常、FastGPT 请求错误都会输出堆栈。

**备份**：只需备份 `STRATUM_DATA_DIR`。`.images/` 是所有知识库共用的图片库，Markdown 与 FastGPT 中的图片链接都指向它，不要删除。每个知识库目录包含 PDF、各版本 Markdown，以及 `.stratum/` 下的版本信息、默认参数、FastGPT 绑定与导入记录、批量记录。`STRATUM_WORK_DIR` 只存临时文件，不需要备份。

**整理文件**：在界面中删除 PDF 见[使用指南](guide.md#删除-pdf)。管理员也可以直接在服务器上移动或删除知识库目录中的文件；这样删除 PDF 时要一并删除同名的 `_vN.md`，FastGPT 中对应的集合需要在 FastGPT 中手动删除。

**资源上限**：`deploy/stratum.service` 默认限制最多 32 核、24 GB 内存。每个解析进程约 350 MB，16 个进程加上服务本身约 8 GB；处理大量扫描件时内存会更高。如果日志中出现进程崩溃且 `pool.restarts` 增长，适当调高 `MemoryMax` 或减少 `STRATUM_WORKERS`。

## 性能参考

以下数据适用于默认的 PyMuPDF + RapidOCR 方案。测试环境为 4 核 CPU，无 GPU。

- 文本型 PDF：每页 0.3 至 1 秒。一份 19 页的英文规范用时 9 秒。
- 扫描型 PDF：每页 5 秒（200 dpi，2 个进程）。

## 许可证

> [!WARNING]
> PyMuPDF 和 pymupdf4llm 使用 AGPL-3.0 许可证。内部使用不受影响。如果把本工具作为对外网络服务提供，必须按 AGPL 开源本工具，或购买 Artifex 商业授权。
