from __future__ import annotations

import copy
import io
import logging
import os
import tempfile
import time
from pathlib import Path

import fitz
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, ImageSequence

from app.errors import DownloadError, ModelUnavailable
from app.result import DocumentResult
from app.vision import MODEL_ID, MODEL_REVISION, backend_name, engine, rewrite_images

logger = logging.getLogger("huahuo-parse")


def _table_header(content: str) -> tuple:
    soup = BeautifulSoup(content, "html.parser")
    row = soup.find("tr")
    return tuple(cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)) if row else ()


def reconstruct(pages, replacements, result, locations):
    """Keep original geometry and every contributing page through official reconstruction."""
    original = [copy.deepcopy(item["res"]) for item in pages]
    structured = engine.restructure(pages)
    originals = {}
    next_id = 0
    for page, location in zip(original, locations):
        blocks = []
        for block in page.get("parsing_res_list", []):
            originals[next_id] = (block, location, page)
            blocks.append({
                "block_id": next_id, "label": block["block_label"],
                "bbox": block["block_bbox"], "content": block["block_content"],
                **({"polygon": block["block_polygon_points"]} if "block_polygon_points" in block else {}),
            })
            next_id += 1
        result.metadata.setdefault("pages", []).append({
            **location, "render_width": page["width"], "render_height": page["height"],
            "coordinate_system": "rendered_image_pixels", "blocks": blocks,
        })
    groups = {}
    for page in structured:
        for block in page["parsing_res_list"]:
            if block.label == "table":
                groups.setdefault(block.global_group_id, []).append(block)
    for group_id, blocks in groups.items():
        sources = [originals[block.global_block_id] for block in blocks]
        review = False
        if len(blocks) > 1:
            # The upstream heuristic can merge unrelated tables with the same column count.
            # Require matching nonempty headers and an actual page-boundary continuation.
            for (left, left_loc, left_page), (right, right_loc, right_page) in zip(sources, sources[1:]):
                header = _table_header(left["block_content"])
                adjacent = right_loc.get("page_number", 0) == left_loc.get("page_number", 0) + 1
                boundary = left["block_bbox"][3] >= left_page["height"] * 0.75 and right["block_bbox"][1] <= right_page["height"] * 0.25
                if not (adjacent and boundary and header and header == _table_header(right["block_content"])):
                    review = True
            # Upstream header deduplication can mistake image-only body rows for headers.
            source_rows = sum(len(BeautifulSoup(source[0]["block_content"], "html.parser").find_all("tr")) for source in sources)
            merged_content = "\n".join(block.content for block in blocks)
            merged_rows = len(BeautifulSoup(merged_content, "html.parser").find_all("tr"))
            image_refs = [image.get("src") for source, _, _ in sources for image in BeautifulSoup(source["block_content"], "html.parser").find_all("img") if image.get("src")]
            if merged_rows < source_rows - (len(blocks) - 1) or any(ref not in merged_content for ref in image_refs):
                review = True
            if review:
                for block, (source, _, _) in zip(blocks, sources):
                    block.content = source["block_content"]
                    block.global_group_id = block.global_block_id
                result.issue("table_continuation_ambiguous", sources[0][1], "跨页表格接续关系待复核，已保留各页分段")
        result.metadata.setdefault("tables", []).append({
            "group_id": group_id, "status": "pending_review" if review else "merged" if len(blocks) > 1 else "single_page",
            "sources": [{**loc, "bbox": block["block_bbox"], "render_width": page["width"], "render_height": page["height"]} for block, loc, page in sources],
        })
    outputs = []
    for page, mapping, location in zip(structured, replacements, locations):
        markdown = rewrite_images(page.markdown["markdown_texts"], mapping)
        if result.metadata.get("file_type") == "pdf" and page["model_settings"].get("use_chart_recognition"):
            # Paddle formats chart blocks as data tables. Keep the selected crops alongside them.
            charts = [f"![原始图表]({block.image['path']})" for block in page["parsing_res_list"] if block.label == "chart" and block.image]
            markdown = "\n\n".join([markdown, *charts])
        # Retain visible provenance for tables moved from later pages by the official merge.
        refs = []
        for table in result.metadata.get("tables", []):
            if table["status"] == "merged" and all(table["sources"][0].get(k) == v for k, v in location.items()):
                numbers = [str(source.get("page_number", "")) for source in table["sources"]]
                refs.append("跨页表格来源：第 " + "、".join(numbers) + " 页")
        outputs.append("\n\n".join([*refs, markdown]).strip())
    return outputs


def parse_visual_file(path: str, extension: str, result: DocumentResult):
    snapshots, replacements, locations = [], [], []

    def consume(image_path, location):
        started = time.perf_counter()
        try:
            snapshot, mapping = engine.page(str(image_path), location, result)
            snapshots.append(snapshot)
            replacements.append(mapping)
            locations.append(location)
        except ModelUnavailable:
            raise
        except Exception as exc:
            result.issue("page_inference_failed", location, f"page inference failed ({type(exc).__name__})")
        logger.info("page completed page=%s elapsed_seconds=%.2f", location.get("page_number"), time.perf_counter() - started)

    with tempfile.TemporaryDirectory(prefix="huahuo-render-") as work:
        rendered = Path(work) / "page.png"
        if extension == ".pdf":
            try:
                document = fitz.open(path)
                if document.needs_pass or not document.page_count:
                    raise ValueError("encrypted or empty PDF")
            except Exception as exc:
                raise DownloadError("PDF pages could not be read; the file may be damaged or password-protected") from exc
            with document:
                result.metadata["page_count"] = document.page_count
                for index in range(document.page_count):
                    page = document[index]
                    location = {"page_number": index + 1, "page_label": page.get_label() or str(index + 1)}
                    try:
                        pix = page.get_pixmap(dpi=int(os.getenv("PDF_RENDER_DPI", "144")), alpha=False)
                        pix.save(rendered)
                        del pix
                        consume(rendered, location)
                    except ModelUnavailable:
                        raise
                    except Exception as exc:
                        result.issue("page_render_failed", location, f"page rendering failed ({type(exc).__name__})")
                    finally:
                        rendered.unlink(missing_ok=True)
        else:
            with Image.open(path) as image:
                result.metadata["page_count"] = getattr(image, "n_frames", 1)
                for index, frame in enumerate(ImageSequence.Iterator(image)):
                    ImageOps.exif_transpose(frame).convert("RGB").save(rendered)
                    consume(rendered, {"page_number": index + 1, "page_label": str(index + 1)})
                    rendered.unlink(missing_ok=True)

    if not snapshots:
        raise DownloadError("all document pages failed to parse")
    result.metadata["parsed_page_count"] = len(snapshots)
    # Never join tables across a failed/missing source page.
    start = 0
    for stop in range(1, len(snapshots) + 1):
        if stop < len(snapshots) and locations[stop]["page_number"] == locations[stop - 1]["page_number"] + 1:
            continue
        texts = reconstruct(snapshots[start:stop], replacements[start:stop], result, locations[start:stop])
        for location, text in zip(locations[start:stop], texts):
            result.parts.append(f"## 第 {location['page_number']} 页\n\n{text}")
        start = stop


def recognize_office_image(data: bytes, location: dict, result: DocumentResult) -> str:
    """Office native fields stay separate from visual evidence."""
    try:
        with Image.open(io.BytesIO(data)) as original:
            buffer = io.BytesIO()
            original.convert("RGB").save(buffer, format="PNG")
            data = buffer.getvalue()
    except Exception as exc:
        result.issue("embedded_image_format", location, f"embedded image format unsupported ({type(exc).__name__})")
        return "[内嵌图片格式暂不支持，待复核]"
    uri = result.asset(data, content_type="image/png", location=location)
    reference = f"![内嵌图片]({uri})\n\n" if uri else ""
    try:
        with tempfile.TemporaryDirectory(prefix="huahuo-office-image-") as work:
            path = Path(work) / "image.png"
            with Image.open(io.BytesIO(data)) as image:
                image.convert("RGB").save(path)
            snapshot, mapping = engine.page(str(path), location, result)
            text = reconstruct([snapshot], [mapping], result, [location])[0]
        return reference + "**视觉识别（" + ", ".join(f"{k}={v}" for k, v in location.items()) + "）**\n\n" + text
    except ModelUnavailable:
        raise
    except Exception as exc:
        result.issue("embedded_image_failed", location, f"embedded image recognition failed ({type(exc).__name__})")
        return reference + "[内嵌图片识别失败，待复核]"


def parse_document(path: str, display_name: str, extension: str, storage, document_id: str) -> dict:
    from app.office import parse_office, parse_text

    result = DocumentResult(document_id, storage)
    result.metadata.update({"file_name": display_name, "file_type": extension.lstrip("."), "encoding": "UTF-8"})
    started = time.perf_counter()
    initial_calls = engine.calls
    if extension in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
        parse_visual_file(path, extension, result)
    elif extension in {".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}:
        parse_office(path, extension, result, recognize_office_image)
    else:
        parse_text(path, extension, result)
    result.metadata["inference"] = {"backend": backend_name(), "model": MODEL_ID, "revision": MODEL_REVISION, "visual_pages": engine.calls - initial_calls}
    result.metadata["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result.finish()
