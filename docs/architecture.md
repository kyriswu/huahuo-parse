# 文档解析架构

保留原 FastAPI 同步接口路径、HTTP `detail` 错误结构、流式下载和本地/OSS 无扩展名产物存储。请求改为必填的 `file_url`、`file_type`。同一服务代码用于 Mac MLX 和 Linux CUDA，环境只选择推理后端。

1. `app/main.py` 校验声明格式与 URL，按原签名目标发送请求，逐跳校验和固定公共 IP，流式限时/限量下载，检查真实文件容器或编码。`finally` 删除下载临时文件。一个信号量限制一个活跃文档。
2. `app/vision.py` 加载单例 PaddleOCR-VL-1.6，打开布局、图表、印章、图片文字识别及 queues，禁止默认忽略页眉页脚。Mac：Paddle CPU + MLX HTTP；Linux：Paddle 原生 gpu:0。布局、页面、VLM 批次为 1，初始化失败显式停止服务。
3. `app/parsing.py` 逐页渲染和消费推理结果，立即删除渲染文件，不持有跨页图像数组；保留 JSON、坐标及渲染尺寸。完成后调用官方跨页重建，校验表格接续，保留全部来源页与歧义分段。失败页不参与跨缺页合并。
4. `app/office.py` 读取 Office 原生结构，保留原数值、公式缓存、表格及备注。图片走同一单例视觉引擎，结果明确标注视觉来源。组合图形通过 LibreOffice 渲染对应幻灯片，包含隐藏页。旧 DOC/PPT 沿用转换器；旧 XLS 沿用 xlrd 的原始值/缓存和合并关系，辅助转换仅恢复公式表达式与图片，不采用重新计算值，失败时报告能力缺口。
5. `app/result.py` 和 `app/storage.py` 先存储引用图像，重写 Markdown 和 HTML 表格图片，再提交最终 UTF-8 Markdown。成功状态只能出现在所有引用均有效之后；上传失败返回部分状态及位置，不输出断链。
6. Docker 多阶段构建固定基础镜像、独立 Linux 依赖锁和公开权重 revision/SHA-256/许可证。上下文白名单排除样本、签名 URL、环境文件和凭证。发布后以 Docker Hub 公开属性、匿名清单拉取及 imagetools 的 linux/amd64/digest 校验结束。

仓库没有异步任务、数据库或跨请求重试状态；继续使用原有 UUID 产物命名空间和同步结果交付。各格式、启动命令、验收及限制见 README。
