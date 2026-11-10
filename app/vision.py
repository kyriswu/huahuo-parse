"""One PaddleOCR-VL-1.6 pipeline shared by PDF pages and Office images."""
from __future__ import annotations

import copy
import io
import logging
import os
import platform
import re
import threading
from pathlib import Path

from PIL import Image

from app.errors import ModelUnavailable

logger = logging.getLogger("huahuo-parse")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
MODEL_ID = "PaddlePaddle/PaddleOCR-VL-1.6"
MODEL_REVISION = "132c45f52c0a26ed5a6b2f6bd8e3985794cc27dd"
LAYOUT_REVISION = "241f8bdfc77a7c7bee915a5057aaee58c235a8d3"
_FIGURE_CAPTION = re.compile(
    r"^\s*(?:(?:附?图|插图)\s*[A-Z]?\s*[\d一二三四五六七八九十]+|fig(?:ure)?\.?\s*[A-Z]?\s*\d)"
    r"|(?:流程图|架构图|示意图|结构图|接线图|现场照片)\s*$",
    re.IGNORECASE,
)
_IMAGE_REFERENCE = re.compile(
    r"""<img\b[^>]*?\s+src\s*=\s*(?P<quote>["'])(?P<html_src>.*?)(?P=quote)[^>]*>"""
    r"|!\[[^\]\n]*\]\((?P<md_src>[^)\n]+)\)",
    re.IGNORECASE,
)
_EMPTY_IMAGE_WRAPPER = re.compile(r'<div style="text-align: center;">\s*</div>')
_OMITTED_IMAGE = "imgs/huahuo-omitted.png"
_FAILED_IMAGE = "imgs/huahuo-upload-failed.png"


def backend_name() -> str:
    return os.getenv("PADDLEOCR_BACKEND", "mlx" if platform.system() == "Darwin" else "cuda")


class VisualEngine:
    def __init__(self):
        self.pipeline = None
        self.lock = threading.RLock()
        self.calls = 0

    def initialize(self):
        with self.lock:
            if self.pipeline is not None:
                return
            backend = backend_name()
            if backend not in {"mlx", "cuda"}:
                raise ModelUnavailable("PADDLEOCR_BACKEND must be mlx or cuda")
            try:
                import paddle
                from paddleocr import PaddleOCRVL
                from paddlex.inference import load_pipeline_config

                if backend == "cuda":
                    if not paddle.is_compiled_with_cuda():
                        raise ModelUnavailable("CUDA backend requires paddlepaddle-gpu; CPU Paddle is installed")
                    if paddle.device.cuda.device_count() < 1:
                        raise ModelUnavailable("CUDA gpu:0 is unavailable; check the NVIDIA driver and --gpus all")
                    paddle.set_device("gpu:0")

                config = copy.deepcopy(load_pipeline_config("PaddleOCR-VL-1.6"))
                config["batch_size"] = 1
                config["use_ocr_for_image_block"] = True
                config["SubModules"]["LayoutDetection"]["batch_size"] = 1
                config["SubModules"]["VLRecognition"]["batch_size"] = 1
                root = Path(os.getenv("MODEL_ROOT", "./var/models"))
                layout = root / "PP-DocLayoutV3"
                vlm = root / "PaddleOCR-VL-1.6"
                if not layout.joinpath("inference.pdiparams").is_file():
                    raise ModelUnavailable("PP-DocLayoutV3 weights missing; run scripts/prepare_models.py")
                kwargs = dict(
                    paddlex_config=config, pipeline_version="v1.6",
                    device="cpu" if backend == "mlx" else "gpu:0",
                    layout_detection_model_dir=str(layout),
                    vl_rec_backend="mlx-vlm-server" if backend == "mlx" else "native",
                    use_doc_orientation_classify=False, use_doc_unwarping=False,
                    use_layout_detection=True, use_chart_recognition=True,
                    use_seal_recognition=True, use_ocr_for_image_block=True,
                    markdown_ignore_labels=[], use_queues=True,
                    cpu_threads=int(os.getenv("PADDLE_CPU_THREADS", "4")), enable_mkldnn=False,
                )
                if backend == "mlx":
                    kwargs.update(
                        vl_rec_server_url=os.getenv("MLX_SERVER_URL", "http://127.0.0.1:8111/"),
                        vl_rec_api_model_name=os.getenv("MLX_MODEL_PATH", str(vlm.resolve())),
                        vl_rec_max_concurrency=1,
                    )
                else:
                    if not vlm.joinpath("model.safetensors").is_file():
                        raise ModelUnavailable("PaddleOCR-VL-1.6 weights missing; run scripts/prepare_models.py")
                    kwargs["vl_rec_model_dir"] = str(vlm)
                self.pipeline = PaddleOCRVL(**kwargs)
                logger.info("model initialized backend=%s device=%s revision=%s", backend, kwargs["device"], MODEL_REVISION)
            except ModelUnavailable:
                raise
            except Exception as exc:
                logger.error("model initialization failed backend=%s error=%s", backend, type(exc).__name__)
                cause = exc
                while cause is not None:
                    if backend == "cuda" and "libcuda.so.1" in str(cause):
                        raise ModelUnavailable(
                            "CUDA driver library libcuda.so.1 is unavailable; check the host NVIDIA driver "
                            "and NVIDIA Container Toolkit, and start the container with --gpus all"
                        ) from exc
                    cause = cause.__cause__ or cause.__context__
                raise ModelUnavailable(
                    f"{backend} model initialization failed ({type(exc).__name__}); check pinned dependencies, weights and backend availability"
                ) from exc

    def page(self, image_path: str, location: dict, result):
        """Consume the generator completely; keep JSON, never the full rendered image."""
        with self.lock:
            self.initialize()
            snapshot = None
            replacements = {}
            for raw in self.pipeline.predict_iter(
                image_path, use_queues=True, markdown_ignore_labels=[],
                use_chart_recognition=True, use_seal_recognition=True,
                use_ocr_for_image_block=True,
            ):
                self.calls += 1
                if "error" in raw:
                    raise RuntimeError("PaddleOCR page inference failed")
                snapshot = copy.deepcopy(raw.json)
                serialized_blocks = snapshot["res"]["parsing_res_list"]
                pdf = result.metadata.get("file_type") == "pdf"
                if pdf:
                    replacements = _select_pdf_assets(raw, serialized_blocks, location, result)
                else:
                    replacements = {
                        name: _save_image(image, result, location)
                        for name, image in raw.markdown.get("markdown_images", {}).items() if image is not None
                    }
                    for block, serialized in zip(raw["parsing_res_list"], serialized_blocks):
                        if block.image:
                            name = block.image["path"]
                            serialized["image"] = {"path": replacements.get(name) or name, "img": None}
                for serialized in serialized_blocks:
                    # Resolve table image references before pages with colliding names are merged.
                    serialized["block_content"] = rewrite_images(serialized["block_content"], replacements)
                if pdf:
                    replacements = {_OMITTED_IMAGE: "", _FAILED_IMAGE: None}
                # Official reconstruction accepts JSON snapshots. These contain no image arrays.
                snapshot["res"]["input_path"] = "document"
                snapshot["res"]["page_index"] = location.get("page_number", 1) - 1
                del raw
            if snapshot is None:
                raise RuntimeError("PaddleOCR returned no page result")
            return snapshot, replacements

    def restructure(self, pages: list[dict]):
        with self.lock:
            self.initialize()
            return self.pipeline.restructure_pages(
                pages, merge_tables=True, relevel_titles=True, concatenate_pages=False
            )


def _has_figure_caption(blocks, index, page_height):
    x1, y1, x2, y2 = blocks[index].bbox
    for caption in blocks[max(0, index - 1):index + 2]:
        if caption.label != "figure_title" or not _FIGURE_CAPTION.search(caption.content):
            continue
        cx1, cy1, cx2, cy2 = caption.bbox
        narrower = min(x2 - x1, cx2 - cx1)
        overlap = min(x2, cx2) - max(x1, cx1)
        gap = max(cy1 - y2, y1 - cy2)
        if narrower > 0 and overlap >= narrower / 2 and -(cy2 - cy1) / 4 <= gap <= page_height * 0.04:
            return True
    return False


def _substantial_visual(block, page_width, page_height):
    """Keep captionless slide visuals without publishing small incidental crops."""
    x1, y1, x2, y2 = block.bbox
    width, height = max(0, x2 - x1), max(0, y2 - y1)
    if page_width <= 0 or page_height <= 0:
        return False
    relative_width, relative_height = width / page_width, height / page_height
    area = relative_width * relative_height
    # PPT exports usually have a landscape canvas. Text/image groups on those
    # slides need not have figure titles. Portrait PDFs can contain pasted slides.
    slide_visual = page_width / page_height >= 1.2 and area >= 0.06 and relative_width >= 0.18 and relative_height >= 0.12
    large_visual = area >= 0.15 and relative_width >= 0.25 and relative_height >= 0.18
    wide_visual = relative_width >= 0.65 and relative_height >= 0.2
    return slide_visual or large_visual or wide_visual


def _select_pdf_assets(raw, serialized_blocks, location, result):
    # Standalone blocks own the published references. Strip raw crop links from block
    # content, including table cells whose OCR text remains inline.
    replacements = dict.fromkeys((asset["path"] for asset in raw["imgs_in_doc"]), "")
    blocks = raw["parsing_res_list"]
    for index, (block, serialized) in enumerate(zip(blocks, serialized_blocks)):
        if block.image:
            replacements[block.image["path"]] = ""
        elif block.label not in {"image", "seal", "chart"}:
            continue

        uri = ""
        if block.label == "chart" or (
            block.label == "image" and (
                _has_figure_caption(blocks, index, raw["height"])
                or _substantial_visual(block, raw["width"], raw["height"])
            )
        ):
            source = {**location, "bbox": block.bbox}
            try:
                image = block.image["img"] if block.image else None
                if image is None:
                    # Some recognized visuals export data without a crop. Slice the
                    # already decoded BGR page at the detector's coordinates.
                    pixels = raw["doc_preprocessor_res"]["output_img"]
                    x1, y1, x2, y2 = (max(0, coordinate) for coordinate in block.bbox)
                    crop = pixels[y1:y2, x1:x2]
                    image = crop[..., ::-1] if crop.size else None
                if image is not None:
                    uri = _save_image(image, result, source, unique_reference=True)
            except (KeyError, OSError, TypeError, ValueError) as exc:
                uri = None
                result.issue("artifact_image_failed", source, f"image artifact could not be prepared ({type(exc).__name__})")

        if block.label == "chart" and not uri:
            # Repeated charts and failed uploads must not retain Paddle's local
            # crop path: reconstruction appends only ready chart artifacts.
            serialized["image"] = None
        else:
            # Paddle's image/seal renderer only emits OCR when an image path exists.
            path = _FAILED_IMAGE if uri is None else uri or _OMITTED_IMAGE
            serialized["image"] = {"path": path, "img": None}
    return replacements


def _save_image(image, result, location, *, unique_reference=False):
    if not isinstance(image, Image.Image):
        image = Image.fromarray(image)
    if image.mode != "RGB":
        image = image.convert("RGB")
    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return result.asset(buffer.getvalue(), content_type="image/png", location=location, unique_reference=unique_reference)


def rewrite_images(markdown: str, replacements: dict) -> str:
    """Rewrite image references without touching OCR text or looping over all crop names."""
    if not replacements:
        return markdown

    def replace(match):
        group = "html_src" if match.group("html_src") is not None else "md_src"
        path = match.group(group)
        if path not in replacements:
            return match.group(0)
        uri = replacements[path]
        if uri is None:
            return "[图片产物保存失败]"
        if not uri:
            return ""
        start, end = match.span(group)
        return match.string[match.start():start] + uri + match.string[end:match.end()]

    return _EMPTY_IMAGE_WRAPPER.sub("", _IMAGE_REFERENCE.sub(replace, markdown))


engine = VisualEngine()
