# Unstructured URL 文档解析 API

独立的 Python 3.11 + FastAPI + Uvicorn + Unstructured 服务。当前锁定 `unstructured==0.14.0`：已验证其 `partition()` 和 PDF `fast` 路径可以在不安装 `unstructured-inference`/Torch 的情况下运行；较新的 0.27.x PDF 加载器会强制导入视觉推理依赖，不符合本服务的轻量约束。`POST /api/v1/parse` 接收 JSON 文档 URL，服务端对每次请求做 DNS/SSRF 校验、限时流式下载到随机临时文件，解析后删除临时文件。

## 启动

```bash
cd /root/huahuo-parse
# 首次安装（系统已有 /root/.local/bin/python3.11）
/root/.local/bin/python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m nltk.downloader punkt averaged_perceptron_tagger

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

后台运行、查看日志、停止和重启：

```bash
./run.sh
 tail -f logs/service.log
./stop.sh
./restart.sh
```

配置示例见 `.env.example`。程序读取环境变量 `PORT`、`MAX_FILE_SIZE_MB`、`DOWNLOAD_TIMEOUT_SECONDS`、`MAX_REDIRECTS` 和 `LOG_LEVEL`；不会自动读取 `.env` 文件。
如需使用 `.env`，可先执行 `set -a; source .env; set +a` 再启动。

## API

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

解析远程文档：

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/parse \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf","include_metadata":true,"include_elements":true}'
```

FastAPI 文档：`/docs`；OpenAPI：`/openapi.json`。返回的 `strategy` 对 PDF 为 `fast`，其他文本/Office 格式为 `native`。默认不启用 OCR、hi_res、GPU 或模型下载；图片在该轻量模式下会明确返回 415，因为原生图片解析需要 OCR 或 hi_res。

`.doc`/`.ppt`/`.xls` 可能需要系统转换工具或额外依赖；当前最小环境不自动安装 LibreOffice。解析失败会返回简短错误，不返回异常堆栈或服务器临时路径。
