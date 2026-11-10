# huahuo-parse 文档解析服务

同步 FastAPI 服务，通过 `POST /api/v1/parse` 下载原件并输出带来源定位的 UTF-8 Markdown。所有请求执行完整流程，不截断 PDF 页数，不接收上游识别预算、文件分类或抽取策略。PDF 和图片统一使用 PaddleOCR-VL-1.6；Office 读取原生字段，并将内嵌图片交给同一个视觉实例。

## API 与结果

```bash
curl -sS http://127.0.0.1:4763/health
curl -sS http://127.0.0.1:4763/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"file_url":"https://example.com/document.pdf","file_type":"pdf"}'
```

只有 `file_url`、`file_type` 两个必填输入。`file_type` 为扩展名（大小写不敏感，可带点）。签名 URL 作为不透明字符串传输，不解码路径、不重排查询参数；下载使用原始请求目标，经过 DNS、重定向、实际连接地址、TLS 证书及文件内容检查。解析器按声明类型选择，OOXML 检查 ZIP 内部类型，旧 Office 检查 OLE 流，图片检查真实编码。超限返回 413，类型不符返回 415，过期/下载失败返回 502，下载超时返回 504，模型不可用返回 503，错误仍采用 `{"detail":"..."}`。

```json
{
  "markdown": "## 第 1 页\n\n章节与表格……\n",
  "metadata": {
    "document_id": "每次请求沿用 UUID 文档命名空间",
    "file_type": "pdf",
    "encoding": "UTF-8",
    "status": "complete",
    "page_count": 1,
    "parsed_page_count": 1,
    "pages": [{"page_number": 1, "render_width": 1191, "render_height": 1684,
      "coordinate_system": "rendered_image_pixels",
      "blocks": [{"block_id": 0, "label": "text", "bbox": [30, 40, 500, 120], "content": "正文"}]}],
    "tables": [],
    "assets": [],
    "issues": [],
    "markdown_object_key": "documents/UUID/markdown"
  }
}
```

正文包含原页序号、Word 节、Sheet 范围或幻灯片编号；PDF 元数据还保留 PDF 自带页标签。跨页表格的 `sources` 列出全部原页、坐标与渲染尺寸。所有页面先消费为无图像数组的 JSON，再调用官方 `restructure_pages(merge_tables=True, relevel_titles=True)`。上游可能按相同列数合并无关表格，因此额外要求明确的页边接续及相同表头；有歧义时恢复各页原表，标记 `pending_review`。识别的脚注、页眉、页脚通过 `markdown_ignore_labels=[]` 保留。

部分失败返回 `metadata.status=partial`，`issues` 包含原因和位置，并附 `warnings`。完全无法读取或所有页失败返回错误。所有引用产物先保存，最后原子提交 UTF-8 Markdown，之后才返回结果。仓库没有异步任务、重试或幂等提交系统；继续使用同步响应和每份文档 UUID，不额外引入数据库或任务队列。

图片沿用本地/OSS 的无扩展名对象键。`metadata.assets` 包含 MIME、SHA-256、来源和对象键。本地可通过 `download_path`（`/artifacts/documents/...`）读取，已有下游继续按对象键解析。配置 `STORAGE_PUBLIC_BASE_URL` 可输出完整图片地址。

PDF 保存版面识别出的图表、同栏邻近且带明确图注的正文图，以及无图题的主要视觉内容：横版幻灯片中的大图、占据页面较大面积的图和嵌入竖版 PDF 的宽幅截图。因此 PPT 转 PDF 不依赖“图题”才能保留配图。图表即使没有识别出数据，也会按坐标从当前页裁出原图。页眉页脚图、印章图、小型装饰图和表格内部裁剪碎片在编码、上传前跳过，已识别的文字、表格和来源坐标继续输出。同一 PDF 内完全相同的图片只保存并引用一次，重复位置记录在该图片的 `locations` 中。下游可用 Markdown 做文字索引，以 `metadata.assets` 为图片引用清单。

## 格式范围

| 格式 | 路径及保留内容 |
| --- | --- |
| PDF | 全部页面逐页渲染、版面分析、VLM；包含文字层 PDF、扫描件、PPT 截图 PDF。 |
| PNG/JPEG/TIFF/BMP | 校验真实图片编码；TIFF 全部帧；与 PDF 共用模型。 |
| DOCX | 原生标题、段落、列表、表格、可读取 OMML 公式、页眉页脚；内嵌图片视觉结果单独标注。 |
| XLSX | Sheet、范围、合并关系、原生数值/类型/数字格式、公式和文件保存的计算值；缺缓存保留公式并标明缺失。服务不重算公式。 |
| PPTX | 幻灯片顺序、原生文字、表格、图表系列/分类、备注、图片；组合图形使用 LibreOffice 整页渲染补充上下文。 |
| DOC/PPT | 沿用既有 LibreOffice 转换读取路线，再进入 DOCX/PPTX 同一流程。 |
| XLS | 沿用 xlrd 读取原始保存值/公式缓存和合并关系，使用 LibreOffice 辅助恢复公式表达式和图片；忽略转换器重新计算的结果。辅助恢复失败时保留原始值并标记部分成功。 |
| TXT/MD/CSV/HTML | UTF-8 原生读取；CSV 输出 HTML 表格，HTML 提取可见文字，不下载外部资源。 |

Office 的原生字段为准，视觉识别以“视觉识别”独立标注，并带对应段落、单元格或幻灯片定位。隐藏幻灯片保留原顺序，并进入组合图形渲染。格式损坏、无法识别的内嵌图、图片上传失败和组合图形渲染失败均报告位置，避免静默丢失。

## Mac 原生 MLX 联调

Apple Silicon / 24 GB；应用和 MLX 服务使用独立原生虚拟环境。布局在 Paddle CPU 上运行，VLM 在 MLX/Metal 上运行。完整锁文件为 `requirements-mac.lock.txt` 与 `requirements-mlx.lock.txt`，核心版本 PaddleOCR 3.7.0、PaddleX 3.7.2、Paddle CPU 3.2.2、MLX-VLM 0.3.11、MLX 0.30.6。

```bash
brew install uv
brew install --cask libreoffice
uv python install 3.12
uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python requirements-mac.lock.txt
uv venv --python 3.12 .venv-mlx
uv pip sync --python .venv-mlx/bin/python requirements-mlx.lock.txt
.venv/bin/python scripts/prepare_models.py --license-file licenses/Apache-2.0.txt
```

权重来自官方公开仓库，VL 固定 revision `132c45f52c0a26ed5a6b2f6bd8e3985794cc27dd`，PP-DocLayoutV3 固定 revision `241f8bdfc77a7c7bee915a5057aaee58c235a8d3`。下载到 `var/models`，保存每个文件 SHA-256、模型卡及 Apache-2.0 许可证；不下载运行时未使用的评测样本。`MLX_MODEL_PATH` 默认指向这一固定版本的本地目录。

两个终端分别启动（官方 `mlx_vlm.server` 入口也可用，以下命令关闭开发热重载）：

```bash
.venv-mlx/bin/python -m uvicorn mlx_vlm.server:app --host 127.0.0.1 --port 8111
```

```bash
PADDLEOCR_BACKEND=mlx MODEL_ROOT=./var/models \
  MLX_SERVER_URL=http://127.0.0.1:8111/ .venv/bin/python -m app.main
```

本机若运行 fake-IP VPN，系统 DNS 可能返回 `198.18.*` 基准测试地址。此时在应用启动命令前添加 `DOWNLOAD_DNS_OVER_HTTPS=https://cloudflare-dns.com/dns-query`。服务仍检查所有返回地址并固定连接到校验后的公共 IP，保留原始 Host、TLS SNI 和证书校验。生产环境默认使用系统 DNS。

真实 OSS 验收仅从临时环境变量读取，不将签名链接写入仓库或镜像：

```bash
# 在当前终端中安全设置完整链接，再执行；不要保存到项目文件。
read -rs 'HUAHUO_TEST_FILE_URL?OSS signed URL: '
export HUAHUO_TEST_FILE_URL
.venv/bin/python scripts/smoke_api.py
unset HUAHUO_TEST_FILE_URL
```

脚本验证实际后端为 MLX、推理调用增加、PDF 页覆盖完整、原页码齐全及块坐标/尺寸存在，记录结果、Markdown 和耗时到忽略提交的 `var/acceptance/`。链接过期时换同一对象的新签名链接。服务日志不记录查询参数，urllib3 的请求目标 DEBUG 日志被禁用。

## Linux / NVIDIA T4 镜像

目标 `ecs.gn6i-c4g1.xlarge`：linux/amd64、4 vCPU、15 GiB 内存、单张 T4 16 GB。Dockerfile 固定 Python 3.12.12 基础镜像 digest；GPU 依赖单独锁在 `requirements-linux.lock.txt`：Paddle GPU 3.2.2 的 CUDA 11.8 官方 wheel 固定 SHA-256，NVIDIA wheel 依赖固定 CUDA runtime 11.8.89、cuDNN 8.9.6.50 等版本。CUDA 运行库由这些 wheel 提供，容器宿主机需 NVIDIA 驱动和 NVIDIA Container Toolkit；不要求宿主机安装 CUDA toolkit。

生产启动明确使用 `gpu:0`、`native`、`use_queues=True`，单进程一份模型、一个活跃文档任务，页面及视觉批次均为 1，CPU 线程为 4。T4 无 BF16 支持，Paddle 的原生识别器按设备能力选择 float32。CUDA 不可用时直接报错，不回落到 CPU。模型随镜像预置，运行时无需下载。Office 读取、LibreOffice、中文字体、PDF 渲染及 Markdown 拼装都在同一容器。

`.dockerignore` 采用白名单，只有 app、Linux 锁文件、模型准备脚本和 Apache-2.0 许可证能进入构建上下文；最后镜像仅 COPY 应用、运行依赖和固定权重，不包含 `.env`、Mac 环境、样本、验收结果、OSS 链接或 Docker 登录文件。

在 T4 主机运行已发布版本：

```bash
export DOCKERHUB_REPO=magiclaw/huahuo-parse
export VERSION_TAG=vl16-20261110T075147Z-07fbffe
docker compose pull
docker compose up -d --no-build --wait --wait-timeout 600
```

也可直接运行：

```bash
docker run -d --name huahuo-parse --gpus all --shm-size=2g \
  --cpus=4 --memory=14g -p 4763:4763 \
  -v huahuo-parse-storage:/app/var/storage \
  "${DOCKERHUB_REPO}:${VERSION_TAG}"
```

`./run.sh` 用于从源码构建和启动；`./stop.sh` 停止；`./restart.sh` 重启。配置沿用 `.env.example` 和可选 `.env`；生产可配置已有 OSS 存储。`/docs` 和 `/openapi.json` 提供当前接口定义。

## 测试与公开发布

```bash
PRELOAD_MODEL=false .venv/bin/python -m unittest discover -s tests -v
uv pip check --python .venv/bin/python
uv pip check --python .venv-mlx/bin/python
```

旧版 32 项测试在迁移前已跑通；迁移后的回归复用原有下载失败/临时文件清理检查和动态 Office 样本，覆盖新请求契约、签名 URL、内容类型不符、101 页完整覆盖、官方跨页表格合并/歧义、HTML 表格图片、产物提交、公式缓存和图表备注。

本次真实 325 页 OSS 样本、最终图片规则及测试记录见 [2026-11-10 验收记录](docs/acceptance-2026-11-10.md)。

复用 Mac 已登录 Docker CLI，唯一标签发布并验证：

```bash
DOCKERHUB_REPO=magiclaw/huahuo-parse \
  VERSION_TAG="vl16-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD)" \
  ./scripts/publish.sh
```

脚本实际执行 `docker buildx build --platform linux/amd64 --tag "$DOCKERHUB_REPO:$VERSION_TAG" --metadata-file var/release/build-metadata.json --push .`，随后查询 Docker Hub 公开属性、匿名拉取远端清单，并执行 `docker buildx imagetools inspect` 及 `--raw`，核对 digest 和 linux/amd64。记录保存在 `var/release/verification.json`，远端校验完成才算发布验收完成。

2026-11-10 已完成公开发布与远端验收：

- 镜像：`docker.io/magiclaw/huahuo-parse:vl16-20261110T075147Z-07fbffe`
- Digest：`sha256:ca2faa5adf7e513cdc7276d2651c6d3438aaf048f47d5dba80118369e6e3dfa6`
- 实际运行架构：`linux/amd64`；清单中的 `unknown/unknown` 项为构建来源证明。
- 最终 `buildx --push` 退出码 0；公开属性、匿名清单/配置访问、远端内容哈希和本次 buildx digest 均通过核对。没有进行 T4 实机推理测试。

网络中断时可用 `scripts/push_registry_chunks.py` 续传同一 OCI 构建产物，再运行发布脚本。它只使用现有 Docker Hub 登录态，不输出凭证或上传签名参数，按 SHA-256 校验导出层，最后才提交清单。完整实测及实际命令见上述验收记录。

官方接口依据：[Apple Silicon 教程](https://www.paddleocr.ai/v3.6.0/en/version3.x/pipeline_usage/PaddleOCR-VL-Apple-Silicon.html)、[PaddleOCR-VL 完整流程](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL.en.md)、[官方 VL 权重](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6)。
