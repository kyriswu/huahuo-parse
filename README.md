# Unstructured URL 文档解析 API

独立的 Python 3.11 + FastAPI + Uvicorn + Unstructured 服务。当前锁定 `unstructured==0.14.0` 和 `PyMuPDF==1.26.7`。PDF 使用 Unstructured 的 `fast` 策略提取原生文本，并使用 PyMuPDF 提取嵌入图片及页面坐标；本服务不启用 `hi_res`、`ocr_only` 或本地 OCR。`POST /api/v1/parse` 接收 JSON 文档 URL，服务端对每次请求做 DNS/SSRF 校验、限时流式下载到随机临时文件，解析后删除临时文件。

## 项目管理与边界

本项目使用 `uv` 管理 Python 3.11 环境和依赖，规范文件为 `pyproject.toml` 与 `uv.lock`；不再以手工 `pip install` 作为项目安装方式。服务是无状态的 URL 文档解析前置服务：不使用数据库，也不负责 RAG 切片、向量化或入库。若未来确实需要跨请求的轻量状态，优先考虑 Redis；当前识别缓存仅为有上限的进程内缓存，多进程之间不共享。

图片会在本服务内完成提取、OSS 保存、确定性过滤、SHA-256 精确去重和可选识别调度。图片说明和原生文本在 `markdown` 中先合并，后续 RAG 服务再进行切片和入库。

## 启动

```bash
cd /root/huahuo-parse
# 首次安装（要求 Python 3.11）
uv python install 3.11
uv sync --locked

uv run uvicorn app.main:app --host 0.0.0.0 --port 4763
```

后台运行、查看日志、停止和重启：

```bash
./run.sh
 tail -f logs/service.log
./stop.sh
./restart.sh
```

配置示例见 `.env.example`。程序读取环境变量 `PORT`、`MAX_FILE_SIZE_MB`、`DOWNLOAD_TIMEOUT_SECONDS`、`MAX_REDIRECTS`、`LOG_LEVEL`、`MAX_PDF_PAGES`、`MAX_PDF_IMAGES`、`MAX_PDF_TOTAL_IMAGE_MB`、`STORAGE_*`、图片过滤阈值、识别预算和视觉适配器配置；启动脚本会自动加载项目根目录 `.env`。
如需使用 `.env`，可先执行 `set -a; source .env; set +a` 再启动。

## API

健康检查：

```bash
curl http://127.0.0.1:4763/health
```

解析远程文档：

```bash
curl -sS -X POST http://127.0.0.1:4763/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf","include_metadata":true,"include_elements":true}'
```

PDF 图片参数：`extract_images` 默认 `true`；`recognize_images` 默认 `false`；还可传 `max_images` 和 `max_image_bytes`。响应中的 `text_parse_status`、`image_extract_status` 和 `recognition_errors` 分别表示三条链路的状态。`markdown` 按 PyMuPDF 的页面坐标（PDF point，左上角原点，单位 point）排序原生文本块和图片引用；复杂双栏、矢量图表和无法从 Unstructured 元素获得坐标的内容不能保证完全恢复阅读顺序。

图片过滤默认使用原始宽高 64×64、页面显示面积比例 0.5%、SHA-256 精确重复资源和至少 3 页重复页眉/页脚启发式。可通过 `filter_images=false` 关闭单次请求过滤，通过 `IMAGE_SIZE_FILTER_ENABLED=false` 全局关闭尺寸规则；图片实例不会因过滤而删除。`recognize_images=true` 时只有 `CANDIDATE` 图片进入识别，`MAX_RECOGNITION_IMAGES` 或请求的 `max_recognition_images` 在外部请求前限制预算。

图片存储使用 `StorageBackend` 抽象。当前 `.env` 已配置为阿里云 OSS，使用 `oss2` 上传；图片默认通过 `STORAGE_UPLOAD_CONCURRENCY=4` 个受控线程并发上传。对象键形如 `pdf-images/<uuid>/page-1-image-1`，故意不包含 `.jpeg`/`.png` 等后缀。接口返回该无后缀对象键作为 `image_uri`，你可以用自己的图片域名拼接该对象键和 `format` 对应的扩展名；实际对象的 `Content-Type` 仍会按图片格式设置。密钥只保存在权限为 600 的 `.env`，不会进入响应、日志或 Git。

视觉适配器默认关闭。配置 `VISION_PROVIDER=http`、`VISION_API_URL`、`VISION_API_KEY`、`VISION_MODEL` 后，`recognize_images=true` 会逐张调用 OpenAI-compatible JSON 视觉接口；适配器实现位于 `app/main.py` 的 `VisionAdapter`，可替换为其他供应商。API Key 只从环境变量读取，不进入响应或日志。

FastAPI 文档：`/docs`；OpenAPI：`/openapi.json`。返回的 `strategy` 对 PDF 为 `fast`，其他文本/Office 格式为 `native`。PDF 请求默认 `extract_images=true`、`recognize_images=false`。图片已上传到配置的 OSS，响应返回不带扩展名的 OSS 对象键；你可以用自己的图片域名拼接该对象键和 `format` 对应的扩展名。视觉 API 未配置时仍可提取图片元数据。

`.doc`/`.ppt`/`.xls` 可能需要系统转换工具或额外依赖；当前最小环境不自动安装 LibreOffice。解析失败会返回简短错误，不返回异常堆栈或服务器临时路径。
