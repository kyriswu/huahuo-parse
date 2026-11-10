# 文档解析服务架构

## 目标

使用 Docker Compose 部署独立的 Python 3.12 + FastAPI 服务，通过 `POST /api/v1/parse` 接收 JSON 文档 URL，安全下载后用 Unstructured 0.14.8 解析，默认 CPU-only、非 OCR，PDF 文字统一使用 Unstructured `fast` 策略，图片按需额外提取。固定 Python 镜像、依赖版本和文本分词资源，不引入 `unstructured-inference`、Torch 或视觉模型。服务监听 `0.0.0.0`，默认端口 4763，仅供本机或已有内网/反向代理访问，不修改防火墙。

## 架构与数据流

1. FastAPI 校验请求体中的 HTTP/HTTPS URL 和布尔选项。
2. 服务解析域名并拒绝 loopback、私有、链路本地、保留地址及云元数据地址；建立连接前和每次重定向后都重新校验 DNS 结果。
3. 使用带超时的 HTTPS 证书校验下载到随机命名的临时文件，流式限制最大字节数（默认 50 MiB），检查响应 Content-Type 和 URL/响应文件名扩展名。
4. PDF 按请求的 `max_pages`（默认 100）选取前 N 页，超出部分跳过。文本、元素、可选图片提取及 Markdown 使用同一份选定页面的 PDF，不区分文本和图片页数上限。PDF 两种模式均只调用一次 `unstructured.partition.auto.partition(filename=..., strategy="fast")`。关闭提图时禁用元数据；开启时仅在内部保留页码及坐标用于插图，不返回元数据。其他格式使用 Unstructured 原生解析且禁用元数据返回。所有临时文件在成功或异常后删除。
5. 按 `extract_images` 选择唯一的正文字段：开启时返回 `markdown`，关闭时返回 `text`；关闭时不生成 Markdown。不序列化或返回元素明细、坐标、图片列表及统计数据，移除元素和元数据开关。Markdown 保留 Unstructured 文字顺序，结合页码、坐标插入图片对象键及可用的 OCR 结果，对象键由下游转换为访问地址。
6. 图片使用 PyMuPDF `extract_image()` 提取嵌入资源，同一 xref 只提取一次；同一 SHA-256 的资源只上传一次，图片实例及各自位置仍保留。旋转页面的图片坐标转换到与 PDFMiner 一致的页面方向。
7. 内容截断或图片处理不完整时才附带 `warnings`，不将提示混入正文；正常完整结果只有一个正文字段。图片上传失败时跳过无效引用。耗时等诊断信息写入服务日志。

## 格式和依赖

锁定 Python 3.12、FastAPI、Uvicorn、`unstructured==0.14.8`、`pdfminer-six==20240706` 及 PDF/DOCX/PPTX/XLSX/CSV/Markdown 所需的格式依赖。PDF 需要 `pdfminer-six`、`pdf2image`、`pikepdf`、Pillow/HEIF 和 OpenCV 的解析支持，但不安装 `unstructured-inference`、Torch、CUDA 或模型。`.doc` 依赖 LibreOffice 转换，未安装时返回清晰错误；图片保持可识别但不启用 OCR/hi_res。所选版本通过实际 fast 解析和依赖检查；Unstructured 0.14.9 起的布局分析导入链需要推理包，较新 pdfminer 移除了旧库引用的异常类导出，因此不跨越这些兼容边界，也不添加运行时补丁。由于 URL 先由服务层下载，Unstructured 原生 `url=` 不直接使用，以确保大小、重定向和 SSRF 约束可验证。

## 错误和安全

空 URL、非 HTTP(S)、解析到内部地址、超时、超大响应、非文档内容、未知扩展名、损坏文档和解析异常分别返回明确 HTTP 状态码和简短 detail，不返回路径、堆栈或文档全文日志。服务不记录完整 URL 查询参数。

## 验证

启动后检查 `/health`、`/docs`、`/openapi.json`；使用公开 PDF URL 验证成功解析和 JSON 序列化，并测试非法 URL、超时、超大文件/Content-Length、未知扩展名、损坏文件和临时文件清理。若服务器没有 DOCX/PPTX/XLSX 样本，记录未测试而不编造结果。
