#!/usr/bin/env python3
"""Real API acceptance. Never print or persist the signed source URL."""
import argparse
import json
import os
import time
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:4763")
    parser.add_argument("--output", default="var/acceptance")
    args = parser.parse_args()
    url = os.environ.get("HUAHUO_TEST_FILE_URL")
    if not url:
        raise SystemExit("HUAHUO_TEST_FILE_URL is required")
    session = requests.Session()
    session.trust_env = False  # macOS system proxies can intercept localhost API requests.
    before = session.get(args.api + "/health", timeout=10).json()
    started = time.perf_counter()
    response = session.post(args.api + "/api/v1/parse", json={"file_url": url, "file_type": "pdf"}, timeout=36000)
    if response.status_code != 200:
        print(json.dumps({"http_status": response.status_code, "detail": response.json().get("detail")}, ensure_ascii=False))
        raise SystemExit(1)
    result = response.json()
    metadata = result["metadata"]
    after = session.get(args.api + "/health", timeout=10).json()
    assert before["inference_backend"] == "mlx" and after["visual_calls"] > before["visual_calls"], "MLX inference was not observed"
    assert metadata["page_count"] == metadata["parsed_page_count"], "PDF page coverage incomplete"
    for number in range(1, metadata["page_count"] + 1):
        assert f"## 第 {number} 页\n" in result["markdown"], f"page {number} marker missing"
    for page in metadata["pages"]:
        assert page["render_width"] > 0 and page["render_height"] > 0
        assert all(len(block["bbox"]) == 4 for block in page["blocks"])
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "result.md").write_text(result["markdown"], encoding="utf-8")
    summary = {"http_status": response.status_code, "elapsed_seconds": round(time.perf_counter() - started, 2),
               "page_count": metadata["page_count"], "parsed_page_count": metadata["parsed_page_count"],
               "markdown_characters": len(result["markdown"]), "tables": len(metadata.get("tables", [])),
               "assets": len(metadata["assets"]), "status": metadata["status"],
               "visual_calls": after["visual_calls"] - before["visual_calls"], "backend": "mlx", "issues": metadata["issues"]}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
