# Unstructured URL 文档解析服务设计

## 目标

在当前空目录创建一个独立 FastAPI 服务，通过 `POST /api/v1/parse` 接收 JSON 文档 URL，安全下载后用 Unstructured 解析，默认 CPU-only、非 OCR、PDF 使用 `fast` 策略。核实官方当前 0.27.35 后发现其 PDF 模块强制导入 `unstructured-inference`，会引入 Torch/视觉栈；为满足轻量约束，锁定仍支持同一 `partition()`/`fast` API 且已实测的 0.14.0。服务监听 `0.0.0.0`，默认端口 8000，仅供本机或已有内网/反向代理访问，不修改防火墙。

## 架构与数据流

1. FastAPI 校验请求体中的 HTTP/HTTPS URL 和布尔选项。
2. 服务解析域名并拒绝 loopback、私有、链路本地、保留地址及云元数据地址；建立连接前和每次重定向后都重新校验 DNS 结果。
3. 使用带超时的 HTTPS 证书校验下载到随机命名的临时文件，流式限制最大字节数（默认 50 MiB），检查响应 Content-Type 和 URL/响应文件名扩展名。
4. 调用 `unstructured.partition.auto.partition(filename=...)`；PDF 显式传 `strategy="fast"`，其他格式不传无效策略参数。所有临时文件在成功或异常后删除。
5. 将元素的 category、text 和可 JSON 序列化 metadata 返回；按原始顺序合并文本并记录实际耗时和策略。

## 格式和依赖

锁定 Python 3.11、FastAPI、Uvicorn、`unstructured==0.14.0` 及 PDF/DOCX/PPTX/XLSX/CSV/Markdown 所需的格式依赖。PDF 需要 `pdfminer-six`、`pdf2image`、`pikepdf`、Pillow/HEIF 和 OpenCV 的解析支持，但不安装 `unstructured-inference`、Torch、CUDA 或模型。`.doc` 依赖 LibreOffice 转换，未安装时返回清晰错误；图片保持可识别但不启用 OCR/hi_res。由于 URL 先由服务层下载，Unstructured 原生 `url=` 不直接使用，以确保大小、重定向和 SSRF 约束可验证。

## 错误和安全

空 URL、非 HTTP(S)、解析到内部地址、超时、超大响应、非文档内容、未知扩展名、损坏文档和解析异常分别返回明确 HTTP 状态码和简短 detail，不返回路径、堆栈或文档全文日志。服务不记录完整 URL 查询参数。

## 验证

启动后检查 `/health`、`/docs`、`/openapi.json`；使用公开 PDF URL 验证成功解析和 JSON 序列化，并测试非法 URL、超时、超大文件/Content-Length、未知扩展名、损坏文件和临时文件清理。若服务器没有 DOCX/PPTX/XLSX 样本，记录未测试而不编造结果。
