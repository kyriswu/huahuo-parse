# Unstructured URL 文档解析 API

独立的 Python 3.12 + FastAPI + Uvicorn + Unstructured 服务。当前锁定 `unstructured==0.14.8` 和 `PyMuPDF==1.26.7`。PDF 的文字始终使用 Unstructured `fast` 提取；开启提图时额外使用 PyMuPDF 提取嵌入图片及其位置，再合成为图文 Markdown；本服务不启用 `hi_res`、`ocr_only` 或本地 OCR。`POST /api/v1/parse` 接收 JSON 文档 URL，服务端对每次请求做 DNS/SSRF 校验、限时流式下载到随机临时文件，解析后删除临时文件。

## 项目管理与边界

本项目使用 Docker Compose 启动，基础镜像固定为 `python:3.12.12-slim-bookworm`（同时锁定 SHA-256 摘要），通过 `pip` 按 `requirements.txt` 安装运行依赖，版本由 `requirements.lock.txt` 锁定，不依赖宿主机 Python 或 `uv`。`requirements.txt` 列出直接依赖，并使用锁文件约束版本；更新依赖时须同步更新锁文件、重新构建和验证镜像。服务是无状态的 URL 文档解析前置服务：不使用数据库，也不负责 RAG 切片、向量化或入库。若未来确实需要跨请求的轻量状态，优先考虑 Redis；当前识别缓存仅为有上限的进程内缓存，多进程之间不共享。

图片会在本服务内完成提取、OSS 保存、确定性过滤、SHA-256 精确去重和可选识别调度。图片说明和原生文本在 `markdown` 中先合并，后续 RAG 服务再进行切片和入库。

## Docker 启动

宿主机需要 Docker Engine 和 Docker Compose 插件（Compose 2.24.0 或更新版本）。

```bash
cd /root/huahuo-parse
# 可选：需要自定义配置或 OSS、视觉 API 密钥时复制后编辑
test -f .env || cp .env.example .env
chmod 600 .env
# 首次构建镜像并在后台启动，等待健康检查通过
./run.sh
```

日常管理：

```bash
docker compose ps
docker compose logs -f --tail=100 api
./stop.sh
./restart.sh
```

`./run.sh` 等价于 `docker compose up -d --build --wait --wait-timeout 120`，代码或依赖改变后再次执行即可构建并更新容器。容器异常退出或 Docker 重启后会自动启动；手动停止后保持停止状态。日志输出到 Docker，自动轮转（每份 10 MB，保留 3 份）。

首次构建需要下载系统和 Python 依赖。若官方源下载较慢，可在 `.env` 中添加 `DEBIAN_MIRROR=https://mirrors.aliyun.com` 和 `PYPI_INDEX_URL=https://mirrors.aliyun.com/pypi/simple` 后重新执行 `./run.sh`；这些选项只影响构建时的下载源，依赖版本仍由锁文件确定。

构建使用 BuildKit 缓存 Python 包下载，缓存不会进入最终镜像。NLTK 文本分词数据从上游固定提交的 CDN 地址下载并校验 SHA-256，随镜像预置。

Compose 默认加载 `.env.example`，再由可选的 `.env` 覆盖配置；`.env` 不会复制到镜像。程序读取 `MAX_FILE_SIZE_MB`、`DOWNLOAD_TIMEOUT_SECONDS`、`MAX_REDIRECTS`、`LOG_LEVEL`、`MAX_PDF_IMAGES`、`MAX_PDF_TOTAL_IMAGE_MB`、`STORAGE_*`、图片过滤阈值、识别预算和视觉适配器配置。PDF 页数范围由请求参数 `max_pages` 统一控制。`.env` 中的 `PORT` 控制宿主机端口（默认 `4763`），容器内固定监听 `4763`；修改配置后执行 `./run.sh` 使其生效。

本地图片存储固定挂载到容器 `/app/var/storage`，使用 Docker 命名卷 `huahuo-parse_storage`，停止、重启或重建容器均保留文件；使用 OSS 时仍按 `STORAGE_ALIYUN_OSS_*` 配置上传。镜像预装 libmagic 和 NLTK 分词数据，无需在首次解析时下载；不安装 LibreOffice 或本地 OCR。

`unstructured==0.14.8` 原生支持 Python 3.12。PDF 无论是否提图，文字均只调用一次 Unstructured `fast`，不会改用 PyMuPDF 提取文字。开启提图时仅在内部使用 Unstructured 元素的页码和坐标插入图片，这些元数据不返回给调用方。不安装 `unstructured-inference`、Torch、CUDA 或视觉模型。OpenCV、pdf2image 等包是 Unstructured 0.14.8 的 PDF 模块导入依赖，保留这些包不会启用视觉识别。

当前验证兼容组合为 `unstructured==0.14.8` 和 `pdfminer-six==20240706`。从 Unstructured 0.14.9 起引入的 PDF 布局分析导入链需要推理组件；pdfminer.six 2025 年及之后版本移除了旧版 Unstructured 使用的 `pdfminer.pdfparser.PSSyntaxError` 导出。升级时必须执行真实 PDF fast 解析和 `pip check`，不能只验证安装成功或健康检查。

在容器环境运行现有测试：

```bash
docker compose run --rm --no-deps -e STORAGE_LOCAL_ROOT=/tmp/test-storage -v "$PWD/tests:/app/tests:ro" api python -m unittest discover -s tests -v
```

## API

健康检查：

```bash
curl http://127.0.0.1:4763/health
```

解析远程文档：

```bash
curl -sS -X POST http://127.0.0.1:4763/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf","max_pages":100,"extract_images":true}'
```

请求参数：

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `url` | 必填 | HTTP/HTTPS 文档地址。 |
| `extract_images` | `true` | 开启 PDF 图片提取并返回 `markdown`；关闭时返回 `text`。其他支持格式也按此选项选择正文字段，当前图片提取仅支持 PDF。 |
| `max_pages` | `100` | 仅 PDF 使用，统一处理前 N 页的文本和图片，必须为正整数。超出部分跳过，不因总页数超限报错；设置值超过总页数时处理全部页面。 |
| `recognize_images` | `false` | 是否使用已配置的外部视觉服务识别图片，并将结果合并进 Markdown。 |
| `max_images` | `100` | 本次提取的图片实例数量上限，范围 0–1000，仍受服务端 `MAX_PDF_IMAGES` 约束。 |
| `max_image_bytes` | `10485760` | 单张图片字节上限，默认 10 MiB，最大 100 MiB；不控制原始文档大小。 |
| `filter_images` | `true` | 是否对图片进行尺寸、重复资源和页眉页脚过滤，以控制识别调度；不删除 Markdown 中的已提取图片。 |
| `max_recognition_images` | `20` | 本次外部识别调用预算，默认可由 `MAX_RECOGNITION_IMAGES` 配置，范围 0–1000。 |

成功响应只包含后续流程需要的正文。开启图片提取时：

```json
{
  "markdown": "第一段正文。\n\n![PDF 第 1 页图片](pdf-images/对象ID/page-1-image-1)\n\n下一段正文。"
}
```

图片引用保留原始存储对象键，不拼接域名、访问地址或扩展名，由后续流程处理。启用并成功完成 OCR 时，识别文本作为引用块紧随对应图片，例如 `> 图片识别文本：识别出的内容`。多行识别结果保留在同一个引用块中。

设置 `extract_images=false` 时只返回：

```json
{
  "text": "第一段正文。\n\n下一段正文。"
}
```

即使没有提取出图片，开启提图模式时也返回 `markdown`。响应不返回元素明细、坐标、图片列表、统计计数或成功标志；不再提供 `include_elements`、`include_metadata` 请求参数，未定义的请求参数返回 HTTP 422。

只有处理范围截断、图片提取/上传不完整或识别失败时，才附带 `warnings`。正常完整处理时不返回该字段。例如只处理 325 页文档中的前 100 页：

```json
{
  "markdown": "前 100 页的图文内容……",
  "warnings": ["PDF contains 325 pages; only the first 100 pages were parsed"]
}
```

`warnings` 不会混入 Markdown 正文；图片上传失败时不输出该图片的无效引用。完全无法下载或打开文档等错误仍通过 HTTP 状态码和 `detail` 返回。

Markdown 保留 Unstructured 文本元素的顺序，结合其内部页码、坐标及 PyMuPDF 图片位置插入图片引用，OCR 结果紧随对应图片。关闭提图时返回同一来源的纯文本。复杂多栏、表格、矢量图表不能保证完整恢复排版。文字统一使用 Unstructured `fast`，不运行本地 OCR。没有文本层的扫描 PDF 不会自动产生 OCR 文字。配置外部视觉服务并开启 `recognize_images` 后，才会识别图片内容。

文档下载大小由 `.env` 的 `MAX_FILE_SIZE_MB` 控制（默认 50 MiB，修改后执行 `./run.sh` 生效），超限返回 HTTP 413。`max_image_bytes` 不会提高文档下载上限。提图同时受 `MAX_PDF_IMAGES`、`MAX_PDF_TOTAL_IMAGE_MB` 约束，超限或跳过图片时通过 `warnings` 告知。

图片存储使用 `StorageBackend`：默认写入本地持久卷，配置 `STORAGE_DRIVER=aliyun_oss` 和 `STORAGE_ALIYUN_OSS_*` 凭据后上传 OSS。上传并发由 `STORAGE_UPLOAD_CONCURRENCY` 控制，默认 4。对象键形如 `pdf-images/<uuid>/page-1-image-1`，不带扩展名；OSS 上传时按图片格式设置 `Content-Type`。密钥通过 `.env` 注入，不进入镜像、响应或日志。

提图使用 PyMuPDF `Document.extract_image()` 直接提取嵌入资源，不进行整页渲染或统一转为 PNG。同一 PDF xref 只提取一次，超大资源跳过后不会再次调用 `extract_image()`；保留每个图片实例的页码和位置。同一内容的图片按 SHA-256 去重后只上传一次，不同位置的 Markdown 引用复用同一个对象键。

当前不引入 PyMuPDF4LLM：它面向完整的 PDF/Markdown 和版面处理，文字来源已固定为 Unstructured 时没有必要再增加一套图文转换流程。评估时的 PyMuPDF4LLM 2.1 还绑定 PyMuPDF 2.1 和 `pymupdf_layout` 2.1，不符合当前仅提取嵌入图片、保持精简依赖的需求。

识别过滤默认使用原始宽高 64×64、页面显示面积比例 0.5%、SHA-256 精确去重和至少 3 页重复页眉/页脚启发式；过滤只控制识别调度，图片仍保留。`filter_images=false` 可关闭单次请求过滤，`IMAGE_SIZE_FILTER_ENABLED=false` 可关闭全局尺寸规则。

视觉适配器默认关闭。配置 `VISION_PROVIDER=http`、`VISION_API_URL`、`VISION_API_KEY`、`VISION_MODEL` 后，请求中设置 `recognize_images=true` 才会调用 OpenAI-compatible JSON 视觉接口。识别结果合并入 Markdown，不单独返回识别明细。默认不安装或运行本地 OCR、视觉模型。

FastAPI 文档：`/docs`；OpenAPI：`/openapi.json`。`.doc`/`.ppt`/`.xls` 可能需要系统转换工具或额外依赖，当前最小环境不自动安装 LibreOffice。错误响应不返回异常堆栈或服务器临时路径。
