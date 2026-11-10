# 2026-11-10 解析服务验收记录

## 代码与环境

开始工作时已将 main 从 `e727a36` 拉取到 `07fbffe`。仓库及父目录没有额外的 AGENTS.md。实现保留同步 FastAPI API、UUID 文档命名空间、现有本地/OSS 对象存储与错误结构。

主要修改：

- `app/main.py`：双必填输入、原始签名请求目标、流式下载、内容校验、临时文件清理、单任务入口与产物读取。
- `app/vision.py`、`app/parsing.py`：共享 PaddleOCR-VL-1.6 实例、MLX/CUDA 配置、全页解析、官方跨页重建、来源坐标与保守图片筛选。
- `app/office.py`：原生 Office 字段、公式缓存、图表/备注、内嵌图片与组合幻灯片渲染。
- `app/result.py`、`app/storage.py`：图片去重、引用解析、最后提交 Markdown 与部分成功位置。
- `Dockerfile`、`.dockerignore`、`compose.yaml`、分环境锁文件、`scripts/`：固定模型与依赖、公开镜像边界、构建发布及验收。
- `tests/test_app.py`、`README.md`、`openapi.json`、`docs/architecture.md`：回归、启动命令与接口说明。

Mac：Apple M5 Pro / 24 GB，原生 Python 3.12。Paddle CPU 做版面分析；MLX/Metal 做 VLM。实际 MLX 服务健康记录包含本地固定 revision 模型路径，运行时记录 `metal_available=true`、`Device(gpu, 0)`。

## 真实 OSS API 验收

完整签名链接只通过临时 `HUAHUO_TEST_FILE_URL` 提供。提交 `file_type=pdf`，调用真实 `POST /api/v1/parse`，没有模拟识别或截页。

| 项目 | 结果 |
| --- | --- |
| 原件 | 电子标书.pdf |
| 大小 | 81,249,929 bytes |
| SHA-256 | `d9c063dfe3f9b8107e7d78c9e27574ac8b23d9dd21096f84e32d9c31b07ac59f` |
| HTTP | 200 |
| 完整请求耗时 | 2,988.36 秒，约 49 分 48 秒 |
| 页覆盖 | 325 / 325，325 次实际视觉调用 |
| 后端 | `mlx-vlm-server`，Metal GPU |
| 可见原页码 | 325 个 |
| 表格分组 | 120 |
| 结果状态 | `partial`，两组表格接续歧义 |

第 33–34 页及第 321–323 页的表格接续关系有歧义，保留逐页分段并标记 `pending_review`，没有将其当作确定的跨页合并。

第 251 页的六列表头“一级指标 / 二级指标 / 指标性质 / 指标内容 / 指标响应 / 是否响应”及“依托社区智慧服务平台”“生活垃圾分类全覆盖”已与原件检查。每页保留原页号、页标签、块坐标和对应渲染尺寸。图片 SHA-256、Markdown 引用和本地产物 HTTP 读取均已检查；下载临时文件剩余 0。

### 图片筛选的补充验收

完整 API 请求运行期间，用户确认保留新增筛选策略，同时补充 PPT 转 PDF 可能没有图题。因此最终规则同时保留图表、带明确图注的图，以及无图题的大图、横版幻灯片主要配图和宽幅截图。小装饰、页眉图及表格内部裁剪碎片不发布，OCR 文本继续保留。

完整请求原始结果为 697,620 字符、942 张产物。将最终筛选及官方后处理应用于**已记录的真实 MLX 结果**后，为 623,924 字符、321 张产物、321 个有效引用；3,197 个原始识别块的文字和坐标不变。这一步是后处理重放，没有声称重新对 325 页推理。

另外从同一原件取第 59、63 页，在最终规则下重新实际调用 MLX：两页完成，保留 3 张没有图题的主要配图。

私有验收记录保存在被 Git 和 Docker 构建上下文排除的 `var/acceptance/`：`summary.json`、`filtered-result.json`、`filtered-result.md`、`filtered-result-qa.json`、`policy-comparison.json`、`captionless-real.json`。原件临时 PDF 已清理。

## 格式和测试

支持 PDF、PNG/JPEG/TIFF/BMP、DOCX/XLSX/PPTX、DOC/XLS/PPT、TXT/Markdown/CSV/HTML。原生 Office 内容及旧版转换路线复用仓库已有动态样本；公式缓存缺失、合并单元格、OMML、内嵌图片、图表与备注、隐藏幻灯片及组合图形渲染有回归覆盖。

迁移前 32 项测试通过。最终 Mac 回归 29 项通过；Linux 镜像回归 28 项通过、1 项因没有宿主 NVIDIA 驱动跳过。另以生产默认启动方式实际运行容器，进程按预期退出并明确报告 `libcuda.so.1` 缺失、NVIDIA 驱动 / Container Toolkit / `--gpus all`。依赖检查、Python 编译、Ruff F 检查和 `git diff --check` 通过。

生产为固定 CUDA 11.8 / cuDNN 8.9.6.50、Paddle GPU 3.2.2、PaddleOCR/PaddleX 3.7.0/3.7.2、`linux/amd64`、`gpu:0`、一份模型和一个活跃解析任务。构建虚拟机没有 NVIDIA 驱动，本记录不包含 T4 实机推理或显存峰值测量。

安装、MLX 服务及应用启动命令见 [README](../README.md#mac-原生-mlx-联调)。

## Docker Hub 远端发布验收

2026-11-10 已完成，最终 `scripts/publish.sh` 退出码 0。仓库为 [magiclaw/huahuo-parse](https://hub.docker.com/r/magiclaw/huahuo-parse)。

| 项目 | 实际结果 |
| --- | --- |
| 完整镜像 | `docker.io/magiclaw/huahuo-parse:vl16-20261110T075147Z-07fbffe` |
| 版本标签 | `vl16-20261110T075147Z-07fbffe` |
| 远端 index digest | `sha256:ca2faa5adf7e513cdc7276d2651c6d3438aaf048f47d5dba80118369e6e3dfa6` |
| 运行镜像 manifest | `sha256:b98115b3a941b62d44de89c101079f6f8b22ab6e5ba79e76ee6ad1e196f5b1cf` |
| 实际运行配置 | `linux/amd64` |
| Docker Hub `is_private` | `false` |
| 匿名访问 | 清单及配置通过；未重新下载全部 5.2 GB 镜像层 |
| buildx 与远端 digest | 一致，远端原始清单、运行 manifest 和 config 的内容哈希均通过 |

`unknown/unknown` 的第二个清单是 buildx 生成的构建来源证明，不是额外的运行架构。

实际最终发布命令：

```bash
DOCKER_CONTEXT=colima-huahuo-parse-build \
  DOCKERHUB_REPO=magiclaw/huahuo-parse \
  VERSION_TAG=vl16-20261110T075147Z-07fbffe \
  PYTHON=.venv/bin/python ./scripts/publish.sh
```

其实际 buildx 构建/打标签/推送命令展开为：

```bash
DOCKER_CONTEXT=colima-huahuo-parse-build docker buildx build \
  --platform linux/amd64 \
  --tag magiclaw/huahuo-parse:vl16-20261110T075147Z-07fbffe \
  --metadata-file var/release/build-metadata.json --push .
```

最初的大层推送遇到本机代理断连。使用 `scripts/push_registry_chunks.py`、现有 Docker CLI 登录态及 OCI 分块接口，预传并确认了 3,255,736,768 bytes 的依赖层和 1,612,282,550 bytes 的模型层；续传状态只写入忽略提交的 `var/release/oci/`。最终上述标准 buildx 推送复用已存在的大层，成功推送其余层、唯一标签及完整清单，推送阶段为 7.5 秒。完整镜像不是 7.5 秒构建或上传完成。

发布脚本随后实际执行的远端验证：

```bash
.venv/bin/python scripts/verify_release.py \
  magiclaw/huahuo-parse:vl16-20261110T075147Z-07fbffe \
  --build-metadata var/release/build-metadata.json
```

验证器以临时空 Docker 认证配置运行以下两个命令，检查公开匿名访问；不修改原登录态：

```bash
docker buildx imagetools inspect magiclaw/huahuo-parse:vl16-20261110T075147Z-07fbffe
docker buildx imagetools inspect --raw magiclaw/huahuo-parse:vl16-20261110T075147Z-07fbffe
```

证据保存在本地忽略提交的 `var/release/verification.json`、`verification.inspect.txt`、`build-metadata.json`、`source-manifest.json`、`publish-final.log` 和 `linux-tests.log`。独立构建使用 `colima-huahuo-parse-build`，没有切换原默认 Docker context 或停止原有服务。
