"""Read native Office structures, using OCR only for visual evidence."""
from __future__ import annotations

import csv
import datetime
import html
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.errors import DownloadError, ModelUnavailable


def scalar(value):
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def escaped(value) -> str:
    return html.escape(str(scalar(value) if value is not None else "")).replace("\n", "<br>")


def html_table(rows) -> str:
    return "<table>\n" + "\n".join("<tr>" + "".join(f"<td>{escaped(cell)}</td>" for cell in row) + "</tr>" for row in rows) + "\n</table>"


def _math(element) -> str:
    """Readable OMML fractions, roots, scripts and text; keep raw XML in metadata."""
    from lxml.etree import QName
    name = QName(element).localname
    children = {QName(child).localname: child for child in element}
    content = lambda key: _math(children[key]) if key in children else ""
    if name == "t":
        return element.text or ""
    if name == "f":
        return r"\frac{" + content("num") + "}{" + content("den") + "}"
    if name == "sSup":
        return "{" + content("e") + "}^{" + content("sup") + "}"
    if name == "sSub":
        return "{" + content("e") + "}_{" + content("sub") + "}"
    if name == "sSubSup":
        return "{" + content("e") + "}_{" + content("sub") + "}^{" + content("sup") + "}"
    if name == "rad":
        return r"\sqrt{" + content("e") + "}"
    if name.endswith("Pr"):
        return ""
    return "".join(_math(child) for child in element)


def parse_docx(path, result, visual):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(path)
    section, paragraph_number = 1, 0
    counters = {}
    result.parts.append("## Word 第 1 节")

    def list_prefix(paragraph):
        properties = paragraph._p.pPr
        numbering = properties.numPr if properties is not None else None
        style = paragraph.style
        while numbering is None and style is not None:
            properties = style.element.pPr
            numbering = properties.numPr if properties is not None else None
            style = style.base_style
        name = paragraph.style.name if paragraph.style else ""
        if numbering is None and "List" not in name:
            return "", None
        level = numbering.ilvl.val if numbering is not None and numbering.ilvl is not None else 0
        num_id = numbering.numId.val if numbering is not None and numbering.numId is not None else name
        fmt, start = ("decimal" if "Number" in name else "bullet"), 1
        if isinstance(num_id, int):
            root = document.part.numbering_part.element
            abstract = root.xpath(f'./w:num[@w:numId="{num_id}"]/w:abstractNumId')
            if abstract:
                definition = root.xpath(f'./w:abstractNum[@w:abstractNumId="{abstract[0].get(qn("w:val"))}"]/w:lvl[@w:ilvl="{level}"]')
                if definition:
                    format_node, start_node = definition[0].find(qn('w:numFmt')), definition[0].find(qn('w:start'))
                    if format_node is not None:
                        fmt = format_node.get(qn('w:val'))
                    if start_node is not None:
                        start = int(start_node.get(qn('w:val')))
        key = (num_id, level)
        counters[key] = counters.get(key, start - 1) + 1
        prefix = "  " * level + ("- " if fmt == "bullet" else f"{counters[key]}. ")
        return prefix, {"num_id": num_id, "level": level, "format": fmt, "index": counters[key]}

    def paragraph_text(paragraph, location):
        text = paragraph.text
        for math in paragraph._p.iter(qn("m:oMath")):
            formula = _math(math)
            text += " $" + formula + "$"
            from lxml import etree
            result.metadata.setdefault("formulas", []).append({"location": location, "latex": formula, "omml": etree.tostring(math, encoding="unicode")})
        images = []
        for blip in paragraph._p.iter(qn("a:blip")):
            rid = blip.get(qn("r:embed"))
            if rid and rid in paragraph.part.related_parts:
                images.append(visual(paragraph.part.related_parts[rid].blob, location, result))
            elif blip.get(qn("r:link")):
                result.issue("linked_image_unavailable", location, "外链图片未嵌入原件，待复核")
        return text, images

    for block in document.element.body:
        if block.tag == qn("w:p"):
            paragraph_number += 1
            paragraph = Paragraph(block, document)
            location = {"section": section, "paragraph": paragraph_number}
            text, images = paragraph_text(paragraph, location)
            style = paragraph.style.name if paragraph.style else ""
            prefix, list_metadata = list_prefix(paragraph)
            if style.lower().startswith("heading") and style.split()[-1].isdigit():
                text = "#" * min(6, int(style.split()[-1]) + 1) + " " + text
            elif prefix:
                text = prefix + text
            result.parts.extend([text, *images])
            result.metadata["sources"].append({**location, "kind": "native_paragraph", "style": style, **({"list": list_metadata} if list_metadata else {})})
            if paragraph._p.pPr is not None and paragraph._p.pPr.sectPr is not None:
                section += 1
                result.parts.append(f"## Word 第 {section} 节")
        elif block.tag == qn("w:tbl"):
            table = Table(block, document)
            rows = []
            table_index = len(result.metadata.setdefault("native_tables", [])) + 1
            images = []
            merges = []
            grid = []
            for row in table.rows:
                column, entries = 1, []
                for tc in row._tr.tc_lst:
                    entries.append((column, tc))
                    column += tc.grid_span
                grid.append(entries)
            for row_index, entries in enumerate(grid, 1):
                cells = []
                for column, tc in entries:
                    from docx.table import _Cell
                    cell = _Cell(tc, table)
                    span = tc.grid_span
                    location = {"section": section, "table": table_index, "row": row_index, "column": column}
                    text_parts = []
                    for paragraph in cell.paragraphs:
                        text, found = paragraph_text(paragraph, location)
                        text_parts.append(text)
                        images.extend(found)
                    merge = tc.vMerge
                    cell_text = escaped("\n".join(text_parts))
                    rowspan = 1
                    if merge == 'restart':
                        for following in grid[row_index:]:
                            continuation = next((other for col, other in following if col == column), None)
                            if continuation is None or continuation.vMerge != 'continue':
                                break
                            rowspan += 1
                    if merge != 'continue':
                        cells.append(f'<td colspan="{span}" rowspan="{rowspan}">{cell_text}</td>')
                    if span > 1 or merge:
                        merges.append({"row": row_index, "column": column, "colspan": span, "vertical_merge": merge})
                rows.append("<tr>" + "".join(cells) + "</tr>")
            result.parts.extend(["<table>\n" + "\n".join(rows) + "\n</table>", *images])
            result.metadata["native_tables"].append({"section": section, "table": table_index, "merges": merges})
    for number, item in enumerate(document.sections, 1):
        for label, container in (("页眉", item.header), ("页脚", item.footer)):
            for index, paragraph in enumerate(container.paragraphs, 1):
                text, images = paragraph_text(paragraph, {"section": number, "part": label, "paragraph": index})
                if text or images:
                    result.parts.extend([f"Word 第 {number} 节{label}：{text}", *images])


def parse_xlsx(path, result, visual, *, original_values=None):
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    formulas = load_workbook(path, data_only=False)
    cached = load_workbook(path, data_only=True)
    try:
        for sheet in formulas:
            values = cached[sheet.title]
            location = {"sheet": sheet.title, "range": sheet.calculate_dimension()}
            metadata = {**location, "merged_ranges": [str(item) for item in sheet.merged_cells.ranges], "cells": []}
            rows = []
            for row in sheet.iter_rows():
                cells = []
                for cell in row:
                    if cell.__class__.__name__ == "MergedCell":
                        continue
                    value = cell.value
                    original = original_values.get((sheet.title, cell.coordinate)) if original_values is not None else None
                    if original is not None and cell.data_type != "f":
                        value = original["value"]
                    native = {"cell": cell.coordinate, "value": scalar(value), "data_type": cell.data_type, "number_format": cell.number_format}
                    if original is not None:
                        native["original_cell_type"] = original["cell_type"]
                    text = escaped(value)
                    if cell.data_type == "f":
                        expression = value if isinstance(value, str) else getattr(value, "text", str(value))
                        saved = original["value"] if original is not None else values[cell.coordinate].value
                        missing = not original["has_cached_value"] if original is not None else saved is None
                        native.update(formula=expression, cached_value=scalar(saved), cached_value_missing=missing)
                        text = escaped(expression) + " → " + (escaped(saved) if not missing else "[计算结果缺失]")
                        if missing:
                            result.issue("formula_cache_missing", {"sheet": sheet.title, "cell": cell.coordinate}, "公式缺少文件内保存的计算结果，已保留公式")
                    if value is not None:
                        metadata["cells"].append(native)
                    span = next((item for item in sheet.merged_cells.ranges if cell.coordinate in item), None)
                    attrs = f' data-cell="{cell.coordinate}"'
                    if span:
                        attrs += f' colspan="{span.max_col - span.min_col + 1}" rowspan="{span.max_row - span.min_row + 1}"'
                    cells.append(f"<td{attrs}>{text}</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            result.parts.append(f"## Sheet：{sheet.title}（{location['range']}）\n\n<table>\n" + "\n".join(rows) + "\n</table>")
            result.metadata.setdefault("sheets", []).append(metadata)
            for index, image in enumerate(sheet._images, 1):
                anchor = getattr(image.anchor, "_from", None)
                cell = f"{get_column_letter(anchor.col + 1)}{anchor.row + 1}" if anchor else "unknown"
                result.parts.append(visual(image._data(), {"sheet": sheet.title, "cell": cell, "image": index}, result))
    finally:
        formulas.close()
        cached.close()


def parse_xls(path, result, visual):
    import xlrd
    from openpyxl.utils import get_column_letter

    workbook = xlrd.open_workbook(path, formatting_info=True, on_demand=True)
    try:
        originals = {}
        legacy_sheets = []
        for sheet in workbook.sheets():
            rows = [sheet.row_values(row) for row in range(sheet.nrows)]
            legacy_sheets.append({
                "sheet": sheet.name, "merged_ranges_zero_based": list(sheet.merged_cells),
                "cells": [{"row": row + 1, "column": col + 1, "value": scalar(sheet.cell_value(row, col)), "cell_type": sheet.cell_type(row, col)} for row in range(sheet.nrows) for col in range(sheet.ncols) if sheet.cell_value(row, col) != ""],
                "rows": rows,
            })
            for row in range(sheet.nrows):
                for column in range(sheet.ncols):
                    kind = sheet.cell_type(row, column)
                    value = sheet.cell_value(row, column)
                    originals[(sheet.name, f"{get_column_letter(column + 1)}{row + 1}")] = {
                        "value": bool(value) if kind == xlrd.XL_CELL_BOOLEAN else value,
                        "cell_type": kind, "has_cached_value": kind not in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK},
                    }
        try:
            with tempfile.TemporaryDirectory(prefix="huahuo-xls-") as work:
                converted = libreoffice(path, work, "xlsx")
                # xlrd remains authoritative for original values/caches. Conversion is
                # used only to recover formula expressions, formatting and images.
                parse_xlsx(converted, result, visual, original_values=originals)
            result.metadata["legacy_reader"] = "xlrd; LibreOffice for formula expressions and embedded images"
            result.metadata["legacy_merged_ranges"] = [{"sheet": item["sheet"], "ranges_zero_based": item["merged_ranges_zero_based"]} for item in legacy_sheets]
        except ModelUnavailable:
            raise
        except DownloadError:
            for sheet in legacy_sheets:
                result.parts.append(f"## Sheet：{sheet['sheet']}\n\n" + html_table(sheet.pop("rows")))
            result.metadata["sheets"] = legacy_sheets
            result.issue("legacy_xls_reader", {"file_type": "xls"}, "xlrd 已读取原始保存值和合并关系；公式表达式和图片恢复失败，待复核")
    finally:
        workbook.release_resources()


def libreoffice(path, output_dir, output_format):
    command = shutil.which("libreoffice") or shutil.which("soffice")
    mac = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if not command and mac.is_file():
        command = str(mac)
    if not command:
        raise DownloadError("LibreOffice is required for legacy Office formats and grouped-slide rendering")
    profile = Path(output_dir) / "profile"
    conversion = output_format
    if output_format == "pdf":
        conversion = 'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}'
    process = subprocess.run([
        command, "-env:UserInstallation=" + profile.as_uri(), "--headless", "--convert-to", conversion,
        "--outdir", str(output_dir), str(Path(path).resolve()),
    ], capture_output=True, timeout=180, check=False)
    target = Path(output_dir) / (Path(path).stem + "." + output_format)
    if process.returncode or not target.is_file():
        raise DownloadError("LibreOffice could not read or render the document")
    return target


def parse_pptx(path, result, visual):
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    presentation = Presentation(path)
    result.metadata["slide_count"] = len(presentation.slides)
    grouped = []
    for number, slide in enumerate(presentation.slides, 1):
        result.parts.append(f"## 幻灯片 {number}")
        slide_meta = {"slide_number": number, "hidden": slide._element.get("show") == "0", "width_emu": presentation.slide_width, "height_emu": presentation.slide_height, "shapes": []}

        def shapes(items, in_group=False):
            for shape in items:
                location = {"slide_number": number, "shape_id": shape.shape_id}
                source = {**location, "bbox_emu": [shape.left, shape.top, shape.left + shape.width, shape.top + shape.height], "kind": str(shape.shape_type)}
                slide_meta["shapes"].append(source)
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    if number not in grouped:
                        grouped.append(number)
                    shapes(shape.shapes, True)
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        prefix = "  " * paragraph.level + ("- " if paragraph.level else "")
                        result.parts.append(prefix + paragraph.text)
                if shape.has_table:
                    table = shape.table
                    rows = []
                    for row in table.rows:
                        cells = []
                        for cell in row.cells:
                            if not cell.is_spanned:
                                cells.append(f'<td colspan="{cell.span_width}" rowspan="{cell.span_height}">{escaped(cell.text)}</td>')
                        rows.append("<tr>" + "".join(cells) + "</tr>")
                    result.parts.append("<table>\n" + "\n".join(rows) + "\n</table>")
                if shape.has_chart:
                    chart = shape.chart
                    series = []
                    for item in chart.series:
                        entry = {"name": item.name, "values": [scalar(value) for value in item.values]}
                        for attr in ("x_values", "y_values", "bubble_sizes"):
                            if hasattr(item, attr):
                                entry[attr] = [scalar(value) for value in getattr(item, attr)]
                        series.append(entry)
                    categories = [category.label for category in chart.plots[0].categories] if chart.plots else []
                    source["chart"] = {"categories": categories, "series": series}
                    result.parts.append("原生图表数据：\n\n```json\n" + json.dumps(source["chart"], ensure_ascii=False) + "\n```")
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    result.parts.append(visual(shape.image.blob, location, result))

        shapes(slide.shapes)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame
            if notes and notes.text.strip():
                result.parts.append(f"### 幻灯片 {number} 备注\n\n" + notes.text)
                slide_meta["notes"] = notes.text
        result.metadata.setdefault("slides", []).append(slide_meta)
    if grouped:
        import fitz
        with tempfile.TemporaryDirectory(prefix="huahuo-slides-") as work:
            try:
                pdf = libreoffice(path, work, "pdf")
                with fitz.open(pdf) as rendered:
                    for number in grouped:
                        pix = rendered[number - 1].get_pixmap(dpi=144, alpha=False)
                        data = pix.tobytes("png")
                        del pix
                        result.parts.append(f"### 幻灯片 {number} 整页视觉上下文\n\n" + visual(data, {"slide_number": number, "scope": "whole_slide"}, result))
            except Exception as exc:
                result.issue("slide_render_failed", {"slides": grouped}, f"grouped-slide rendering failed ({type(exc).__name__})")


def parse_office(path, extension, result, visual):
    try:
        if extension in {".doc", ".ppt"}:
            # Preserve the repository's LibreOffice-based legacy reader, then use the same native path.
            target = "docx" if extension == ".doc" else "pptx"
            with tempfile.TemporaryDirectory(prefix="huahuo-legacy-") as work:
                converted = libreoffice(path, work, target)
                result.metadata["legacy_reader"] = "LibreOffice"
                parse_office(str(converted), "." + target, result, visual)
        elif extension == ".docx":
            parse_docx(path, result, visual)
        elif extension == ".xlsx":
            parse_xlsx(path, result, visual)
        elif extension == ".xls":
            parse_xls(path, result, visual)
        elif extension == ".pptx":
            parse_pptx(path, result, visual)
    except DownloadError:
        raise
    except Exception as exc:
        raise DownloadError(f"native {extension.lstrip('.')} parsing failed ({type(exc).__name__})") from exc


def parse_text(path, extension, result):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DownloadError("text document must use UTF-8 encoding") from exc
    if extension == ".csv":
        result.parts.append("## CSV\n\n" + html_table(csv.reader(io.StringIO(text))))
    elif extension in {".html", ".htm"}:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        # Keep table cells and heading order readable without fetching external resources.
        result.parts.append(soup.get_text("\n", strip=True))
    else:
        result.parts.append(text)
    result.metadata["sources"].append({"kind": "native_text", "line_start": 1, "line_end": len(text.splitlines())})
