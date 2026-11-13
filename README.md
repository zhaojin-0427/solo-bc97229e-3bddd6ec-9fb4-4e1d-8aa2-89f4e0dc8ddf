# 档案 OCR 跨页检索服务（FastAPI + SQLite）

为档案数据工程师提供的检索 API：研究者用一段 OCR 文字就能定位扫描件中的
**原句**，即使该句子跨块、跨页切分，也能返回可直接用于高亮的块 ID、矩形
坐标与原文字符范围。

## 能力对照

| 需求 | 实现 |
| --- | --- |
| 按文档上传 UTF-8 NDJSON，行序即阅读顺序 | `PUT /documents/{doc_id}`，逐块流式接收，字段：`block_id`（文档内唯一）、`page`、`text`、`rect{x0,y0,x1,y1}` |
| 查询去空白后 1–80 字符，原文匹配也忽略空白 | 服务端对查询与存储文本统一做 Unicode 空白剥离后匹配 |
| 允许跨块、跨页，重叠命中分别保留 | 倒排表含跨块边界二元组；每个位置都是独立行，重叠命中天然各自保留 |
| 返回最早 100 处命中 | 按阅读顺序（全局紧凑字符位置）返回，`has_more` 表示还有第 101 处真命中 |
| 命中含块 ID、坐标、原文字符范围 | 每个 hit 的 `spans[]` 给出 `block_id/page/rect/char_start/char_end` |
| 文档可大于内存 | 接收、建索引、搜索均流式/分批，不载入整篇文本或全部位置映射（见下文设计） |
| 整篇重传更正，后台建索引期间旧版仍可查 | 新版本独立暂存表，旧版本持续可查；就绪后单事务原子切换 |
| 原文、位置映射、索引一起切换 | 切换只在三者全部落盘后发生，单事务更新目录指针 |
| 同文档同时只接受一次替换 | 进程内 asyncio 锁 + SQLite 目录状态双重保证，冲突返回 409 |
| 坏行 / 写盘失败 / 进程中断保留旧版 | 失败版本标记 `failed` 并清理暂存表；启动恢复把未完成版本统一标失败 |
| 慢检索与更正发布并发互不破坏 | 检索先**钉住（pin）**所选版本再开读快照；旧版表的回收推迟到最后一个钉住它的检索结束，绝不会对正在读的版本 `DROP TABLE`（见下文「检索与旧版回收的竞态」） |
| 可启动的 Docker Compose + 交互式文档演示 | `docker compose up` 后访问 Swagger UI，含一键演示数据 |

## 快速开始（Docker Compose）

```bash
# 1. 启动 API（自动构建镜像，SQLite 数据存放在命名卷 archive-data）
docker compose up --build -d

# 2. 灌入演示文档（gazette 公报、ledger 税册，幂等，可重复执行）
docker compose --profile demo run --rm seed

# 3. 打开交互式 API 文档
#    Swagger UI:  http://localhost:8000/docs
#    ReDoc:       http://localhost:8000/redoc
```

在 Swagger UI 中可直接试：

1. `PUT /documents/{doc_id}` —— 上传/替换文档，`doc_id` 填 `gazette`，
   Request body 选 `application/x-ndjson`，粘贴 `demo/gazette.ndjson` 内容；
2. `GET /documents/{doc_id}/search` —— `doc_id=gazette`，试这些查询：
   - `可用。第二條` —— **跨第 1、2 页**的命中，`spans` 含两页的块与坐标；
   - `轉型，保障` —— 同页跨块命中；
   - `檢索 系統應當 忽略排版空白` —— 查询自带任意空白仍命中；
   - `檔` —— 单字查询，多处重叠/重复命中按顺序返回；
3. 再次执行 `PUT` 粘贴修改后的 NDJSON，即完成整篇更正；
   `GET /documents/gazette/versions` 可看到旧 `ready` 版本与新版本。

不用 Docker 时（需 Python 3.11 并能安装依赖）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
ARCHIVE_API_BASE=http://localhost:8000 python -I scripts/seed_demo.py
ARCHIVE_API_BASE=http://localhost:8000 python -I scripts/demo_requests.py
```

## NDJSON 输入格式

每行一个 JSON 对象，UTF-8 编码；空行忽略；行顺序即阅读顺序：

```json
{"block_id": "gazette-p1-b4", "page": 1, "text": "保障電子文檔的真實、完整與可用。", "rect": {"x0": 96.2, "y0": 199.8, "x1": 470.5, "y1": 232.0}}
```

- `block_id`：该文档内非空且唯一，重复即整次上传失败（400）；
- `page`：正整数页码（扫描页，1 起）；
- `text`：OCR 原文，空白原样保留（用于高亮回映），匹配时忽略所有空白；
- `rect`：扫描件上的矩形，`x1>=x0`、`y1>=y0`，允许零面积矩形。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查（含 Compose 健康检查） |
| `GET` | `/documents` | 文档清单及当前生效版本 |
| `PUT` | `/documents/{doc_id}` | 整篇上传/替换（202，后台建索引）；坏行 400、写盘失败 507、并发替换 409 |
| `GET` | `/documents/{doc_id}/search?q=...` | 检索，去空白后 1–80 字符；未知文档 404 |
| `GET` | `/documents/{doc_id}/versions` | 版本与状态（ready/failed/ingesting/indexing） |
| `GET` | `/recovery` | 本次启动完成的崩溃恢复记录 |

命中结构（节选）：

```json
{
  "compact_start": 44, "compact_end": 50,
  "matched_text": "可用。第二條",
  "crosses_blocks": true, "crosses_pages": true,
  "spans": [
    {"block_id": "gazette-p1-b4", "page": 1,
     "rect": {"x0": 96.2, "y0": 199.8, "x1": 470.5, "y1": 232.0},
     "char_start": 13, "char_end": 16, "block_order": 3},
    {"block_id": "gazette-p2-b1", "page": 2,
     "rect": {"x0": 96.2, "y0": 110.0, "x1": 520.4, "y1": 142.2},
     "char_start": 0, "char_end": 3, "block_order": 4}
  ]
}
```

`char_start/char_end` 是该块**原始 `text`**（含空白）上的半开区间，前端可直接
切片高亮；矩形坐标可用于扫描件上画框。

## 设计要点：为什么大文档不会撑爆内存

- **接收**：`request.stream()` 分块进入有界队列（默认 32 块），工作线程逐行
  增量 UTF-8 解码与 JSON 解析，单行超过 4MB 直接判失败；
- **建索引**：每读一块就增量计算紧凑字符位置，按批（默认 500 块）写入该版本
  独立的三张表，绝不累积整篇文本：
  - `blocks_<vid>`：原文、矩形、每块紧凑起止位置（回表与字符回映）；
  - `unigrams_<vid>(ch, pos)`：单字倒排；
  - `bigrams_<vid>(bg, pos)`：二元组倒排，含**跨块边界二元组**（含跨页）；
- **搜索**：单字查询走 unigram；多字查询选取频次最低的二元组，按位置有序
  游标读取候选，逐个只回表读取命中跨度涉及的少数块做验证，单查询只读一致
  快照，不加载全量位置映射；
- **内存自测**：`tests` 中用 2 万块的文档断言 `tracemalloc` 峰值 < 4MB。

## 版本、原子切换与崩溃恢复

- 每次替换创建新版本号与独立暂存表，状态机
  `ingesting → indexing → ready`（异常 → `failed`）；
- 只有 `ready` 版本可被检索；激活在单事务内翻转 `documents.active_version_id`。
  表的**回收与目录翻转解耦**：激活只翻指针，旧版表交给回收器（见下节），
  读者始终看到完整的一版；
- 坏行、重复块 ID、写盘异常都在请求内把新版本标 `failed` 并清表，旧版不动；
- 进程被杀死后，重启时 `startup_recovery()` 把所有非终态版本标 `failed`、
  删除残留暂存表，并回收任何「非当前激活版本」遗留的表（例如慢检索钉住
  旧版、回收被推迟期间进程被杀），通过 `/recovery` 暴露处理记录。

## 检索与旧版回收的竞态（为什么不再 `no such table`）

问题时序：检索请求读到目录指针、**选定旧版本 V1**，但还没来得及读任何
文字；此时一篇更正上传完成，单事务把指针翻到 V2，并在第二个事务里
`DROP` 掉 V1 的表。SQLite 的只读快照要到事务内第一条语句才建立，于是
旧检索再去读 V1 就报 `no such table`（或读到错版、漏掉命中）。

修复由 `app/reaper.py` 的进程内**版本回收器（VersionReaper）**协调，只保存
整数计数，不复制任何文档数据，内存有界：

1. 检索在打开读快照**之前**先对所选版本 `pin(vid)`；
2. 更正发布只做目录翻转，然后对旧版本调用 `retire(old_vid, drop)`：
   - 此刻没有检索钉住它 → 立即删表；
   - 有检索钉住它 → 记录为待回收，**发布不被慢检索阻塞**，新请求立刻可查 V2；
3. 检索在 `finally` 中 `unpin(vid)`；最后一个 pin 释放时，延迟的删表当场执行，
   因此旧表不会被永久保留；
4. 已进入回收（`retired`/已删）的版本拒绝新的 pin（返回 `False`），端点随即
   重读一次目录指针、改查新的激活版本，而不是报错或读旧版；
5. `search()` 把 `BEGIN DEFERRED` 提前到第一次访问表之前，整次查询的紧凑长度、
   倒排读取、候选校验、命中与高亮坐标都来自**同一版本的同一快照**。

由此保证：请求一旦选定版本，其结果始终来自该完整版本；慢检索未结束时更正
照常发布、新请求查到新版；旧请求结束后过时表立即回收。崩溃等极端情况下
未及执行的延迟删除，由启动恢复兜底清理。

## 测试

```bash
# 标准环境（有 fastapi/pytest 时）
pytest -q

# 离线/无包索引环境：stdlib 垫片 + unittest（本仓库沙箱即此模式）
python -I scripts/run_tests.py     # 单元/集成/竞态回归测试
python -I scripts/smoke_test.py    # 真实 HTTP 端到端冒烟（守护线程内 WSGI）
```

`tests/test_interleave.py` 用 `threading.Event` 屏障**可重复地**控制交错时序，
覆盖：检索已选定旧版但未读文字时更正发布并回收（不再 `no such table`、
命中与高亮坐标仍来自旧版、读完即回收旧表）、指针翻转发生在「读指针→pin」
间隙时端点改查新版、慢检索不阻塞发布、并发读者与反复替换的压力测试，以及
进程重启后回收延迟删除遗留的表。`tests/_shim/` 仅是离线环境对
fastapi/pydantic 的最小垫片，不进入 Docker 镜像（见 `.dockerignore`），
生产代码只依赖 `requirements.txt` 中的真实组件。

## 目录结构

```
app/            FastAPI 应用、SQLite 目录、流式入库、检索引擎、版本回收器、响应模型
demo/           两份中文 NDJSON 示例文档
scripts/        演示播种、演示请求、测试运行器与离线冒烟
tests/          单元/集成、竞态交错回归、属性测试与离线垫片
Dockerfile, docker-compose.yml, requirements.txt
```
