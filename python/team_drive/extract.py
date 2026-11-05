"""Bounded, read-only extraction into version-independent source locators.

The caller owns snapshot consistency, wall-clock/process memory limits, and
publication of partial results. No archive member is extracted to disk.
"""
from __future__ import annotations

import csv
import heapq
import json
import logging
import posixpath
import re
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterator

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".csv", ".tsv", ".xlsx", ".docx", ".pptx", ".pdf"})
DEFAULT_LIMITS = {
    "max_file_bytes": 256 * 1024 * 1024,
    "max_uncompressed_bytes": 512 * 1024 * 1024,
    "max_segments": 100_000,
    "max_segment_chars": 8_000,
    "max_columns": 512,
    "max_pdf_pages": 2_000,
}
NS = {
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


class ExtractionError(Exception):
    """Public errors contain stable codes and no filesystem paths."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _limits(given: Dict[str, Any]) -> Dict[str, int]:
    result = dict(DEFAULT_LIMITS)
    for key in result:
        if key in given:
            value = given[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ExtractionError("invalid_limits", "Extraction limits must be positive integers.")
            result[key] = value
    return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _segment(locator, text, data=None, warnings=None):
    return {"locator": locator, "text": text, "data": data, "warnings": sorted(set(warnings or []))}


def _text_segments(text, locator, limit, warnings=None, data=None):
    # Parts are explicit: consumers must not mistake one part for a whole page.
    total = max(1, (len(text) + limit - 1) // limit)
    for part in range(total):
        loc = dict(locator, part=part + 1, parts=total)
        yield _segment(loc, text[part * limit:(part + 1) * limit], data, warnings)


class _Archive:
    def __init__(self, path: Path, limits):
        with path.open("rb") as handle:
            if handle.read(8) == bytes.fromhex("d0cf11e0a1b11ae1"):
                raise ExtractionError("encrypted", "Encrypted or legacy binary Office containers are not supported.")
        self.zip = zipfile.ZipFile(path)
        try:
            infos = self.zip.infolist()
            if len(infos) > limits["max_segments"] * 10:
                raise ExtractionError("limit_exceeded", "Archive contains too many entries.")
            names = [i.filename for i in infos]
            if len(names) != len(set(names)):
                raise ExtractionError("parse_failed", "Archive has ambiguous duplicate entries.")
            total = 0
            for info in infos:
                name = info.filename
                if name.startswith("/") or "\\" in name or ".." in name.split("/"):
                    raise ExtractionError("unsafe_archive", "Archive has an unsafe member name.")
                if info.flag_bits & 1:
                    raise ExtractionError("encrypted", "Encrypted archives are not supported.")
                total += info.file_size
                if info.file_size > limits["max_uncompressed_bytes"] or total > limits["max_uncompressed_bytes"]:
                    raise ExtractionError("limit_exceeded", "Archive expansion exceeds the configured byte limit.")
            self.names = set(names)
        except Exception:
            self.zip.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.zip.close()

    def tree(self, name):
        with self.zip.open(name) as handle:
            return SafeET.parse(handle, forbid_dtd=True, forbid_entities=True, forbid_external=True).getroot()

    def events(self, name):
        with self.zip.open(name) as handle:
            stack = []
            for event, elem in SafeET.iterparse(handle, events=("start", "end"), forbid_dtd=True,
                                               forbid_entities=True, forbid_external=True):
                if event == "start":
                    stack.append(elem)
                    yield event, elem
                else:
                    had_content = bool(elem.attrib) or len(elem) > 0 or elem.text is not None
                    yield event, elem
                    # Callers clear complete units after processing. Detach those
                    # units as well, so millions of empty row/si elements do not
                    # accumulate in the iterparse root.
                    if had_content and not elem.attrib and len(elem) == 0 and elem.text is None and len(stack) > 1:
                        stack[-2].remove(elem)
                    stack.pop()

    def relationships(self, part):
        folder, filename = posixpath.split(part)
        relpart = posixpath.join(folder, "_rels", filename + ".rels")
        if relpart not in self.names:
            return {}
        result = {}
        for rel in self.tree(relpart):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            if ":" in target or "\\" in target:
                continue
            resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(folder, target))
            if resolved.startswith("../") or resolved not in self.names:
                continue
            result[rel.get("Id")] = (resolved, rel.get("Type", ""))
        return result


def _lines(path, limits):
    cap = limits["max_segment_chars"]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        line, part = 1, 1
        previous_cr = False
        while True:
            # readline(size) also bounds single exceptionally long lines.
            text = handle.readline(cap)
            if not text:
                break
            # A size boundary can split CRLF. Keep its LF on the same source
            # line; a bare CR starts the next line before its following text.
            if previous_cr and not text.startswith("\n"):
                line += 1
                part = 1
            yield _segment({"kind": "line", "line_start": line, "line_end": line, "part": part}, text)
            if text.endswith("\n"):
                line += 1
                part = 1
            else:
                part += 1
            previous_cr = text.endswith("\r")


def _column(index):
    out = ""
    while index:
        index, mod = divmod(index - 1, 26)
        out = chr(65 + mod) + out
    return out


def _column_number(coordinate):
    match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", coordinate)
    if not match:
        raise ExtractionError("parse_failed", "Spreadsheet contains an invalid cell coordinate.")
    result = 0
    for char in match.group(1):
        result = result * 26 + ord(char) - 64
    return result


def _cell_text(cell):
    value = cell.get("value")
    pieces = ["" if value is None else str(value)]
    if cell.get("formula") is not None:
        pieces.append("formula=" + str(cell["formula"]))
    return cell["coordinate"] + ": " + " | ".join(pieces)


def _row_segments(cells, locator, limit, warnings, metadata=None):
    """Retain every cell; split oversized values explicitly instead of truncating."""
    groups = []
    size = 0
    part = 0
    for cell in cells:
        # Bound string payload fields as well as the searchable text.
        strings = {key: value for key, value in cell.items()
                   if key in {"value", "formula", "cached_value"} and isinstance(value, str)}
        slice_cap = max(1, (limit - 64) // max(1, len(strings)))
        pieces = max([1] + [(len(value) + slice_cap - 1) // slice_cap for value in strings.values()])
        for cell_part in range(pieces):
            item = dict(cell)
            if pieces > 1:
                for key, value in strings.items():
                    item[key] = value[cell_part * slice_cap:(cell_part + 1) * slice_cap]
                item["value_part"] = cell_part + 1
                item["value_parts"] = pieces
            text = _cell_text(item)
            if len(text) > limit:
                raise ExtractionError("limit_exceeded", "A cell locator and payload exceed the configured segment size.")
            if groups and size + len(text) + 1 > limit:
                part += 1
                yield _segment(dict(locator, part=part, column_start=groups[0]["coordinate"],
                                    column_end=groups[-1]["coordinate"]),
                               "\n".join(_cell_text(c) for c in groups),
                               dict(metadata or {}, cells=groups), warnings)
                groups, size = [], 0
            groups.append(item)
            size += len(text) + 1
    if groups or not cells:
        part += 1
        yield _segment(dict(locator, part=part,
                            column_start=groups[0]["coordinate"] if groups else None,
                            column_end=groups[-1]["coordinate"] if groups else None),
                       "\n".join(_cell_text(c) for c in groups),
                       dict(metadata or {}, cells=groups), warnings)


def _csv(path, limits):
    # csv's limit is process-global, so restore it when the generator is closed.
    previous = csv.field_size_limit()
    csv.field_size_limit(limits["max_file_bytes"])
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ",", strict=True)
            for row_number, row in enumerate(reader, 1):
                if len(row) > limits["max_columns"]:
                    raise ExtractionError("limit_exceeded", "Delimited file exceeds the configured column limit.")
                cells = [{"coordinate": _column(i) + str(row_number), "value": value,
                          "value_type": "string"} for i, value in enumerate(row, 1)]
                yield from _row_segments(cells, {"kind": "row", "sheet": "data", "row": row_number},
                                         limits["max_segment_chars"], [], {"physical_line_end": reader.line_num})
    finally:
        csv.field_size_limit(previous)


def _xlsx(path, limits):
    with _Archive(path, limits) as arc, tempfile.TemporaryDirectory(prefix="team-drive-strings-") as temp:
        # A disk-backed shared-string lookup avoids a Python list proportional to
        # every string in a large workbook. Temporary data is deleted on exit.
        # sqlite3's connection context manages transactions, not the connection
        # lifetime. Close before TemporaryDirectory cleanup (required on Windows).
        with closing(sqlite3.connect(str(Path(temp) / "strings.sqlite"))) as strings, strings:
            strings.execute("CREATE TABLE strings (id INTEGER PRIMARY KEY, text TEXT NOT NULL)")
            if "xl/sharedStrings.xml" in arc.names:
                counter = 0
                for event, elem in arc.events("xl/sharedStrings.xml"):
                    if event == "end" and _local(elem.tag) == "si":
                        value = _spreadsheet_text(elem)
                        strings.execute("INSERT INTO strings VALUES (?, ?)", (counter, value))
                        counter += 1
                        elem.clear()
            formats = {}
            styles = []
            if "xl/styles.xml" in arc.names:
                root = arc.tree("xl/styles.xml")
                formats = {x.get("numFmtId"): x.get("formatCode") for x in root.findall("s:numFmts/s:numFmt", NS)}
                styles = [x.get("numFmtId", "0") for x in root.findall("s:cellXfs/s:xf", NS)]
            workbook = arc.tree("xl/workbook.xml")
            if workbook.tag != "{" + NS["s"] + "}workbook":
                raise ExtractionError("unsupported_structure", "Unsupported spreadsheet XML namespace.")
            date1904 = workbook.find("s:workbookPr", NS)
            date1904 = date1904 is not None and date1904.get("date1904") in {"1", "true"}
            rels = arc.relationships("xl/workbook.xml")
            sheets = workbook.findall("s:sheets/s:sheet", NS)
            if not sheets:
                raise ExtractionError("unsupported_structure", "Workbook has no supported worksheet declarations.")
            for sheet in sheets:
                relation = rels.get(sheet.get("{" + NS["r"] + "}id"))
                if not relation:
                    raise ExtractionError("parse_failed", "A worksheet relationship is missing or unsupported.")
                filename = relation[0]
                metadata = {"sheet_state": sheet.get("state", "visible"), "date_system": "1904" if date1904 else "1900"}
                # Merges generally occur after sheetData; preflight before rows
                # so every affected result has an explicit interpretation warning.
                merged, hidden_columns, has_drawings = [], [], False
                for event, elem in arc.events(filename):
                    if event != "end":
                        continue
                    tag = _local(elem.tag)
                    if tag == "mergeCell":
                        if len(merged) >= limits["max_segments"]:
                            raise ExtractionError("limit_exceeded", "Worksheet has too many merged ranges.")
                        merged.append(elem.get("ref"))
                    elif tag == "col" and elem.get("hidden") in {"1", "true"}:
                        hidden_columns.append({"min": elem.get("min"), "max": elem.get("max")})
                        if len(hidden_columns) > limits["max_columns"]:
                            raise ExtractionError("limit_exceeded", "Worksheet has too many column metadata ranges.")
                    elif tag in {"drawing", "legacyDrawing"}:
                        has_drawings = True
                    elem.clear()
                warnings = ["number_formats_not_rendered", "external_links_not_resolved"]
                if merged:
                    warnings.append("merged_cells_not_filled")
                if has_drawings:
                    warnings.append("images_and_charts_not_extracted")
                # Sweep merged ranges as rows advance instead of scanning every
                # merge for every row (which becomes quadratic on large sheets).
                merge_intervals = []
                for ref in merged:
                    row_numbers = [int(value) for value in re.findall(r"[A-Z]+([0-9]+)", ref or "")]
                    if not row_numbers:
                        raise ExtractionError("parse_failed", "Worksheet contains an invalid merged range.")
                    merge_intervals.append((min(row_numbers), max(row_numbers), ref))
                merge_intervals.sort()
                merge_cursor, previous_row = 0, 0
                active_merges, merge_ends = {}, []
                for event, elem in arc.events(filename):
                    if event != "end" or _local(elem.tag) != "row":
                        continue
                    number = int(elem.get("r", "0"))
                    if number < 1:
                        raise ExtractionError("parse_failed", "Worksheet row number is missing.")
                    if number <= previous_row:
                        raise ExtractionError("parse_failed", "Worksheet rows are duplicate or out of order.")
                    previous_row = number
                    row_warnings = list(warnings)
                    cells = []
                    seen_coordinates = set()
                    for node in elem.findall("s:c", NS):
                        coordinate = node.get("r", "")
                        if _column_number(coordinate) > limits["max_columns"]:
                            raise ExtractionError("limit_exceeded", "Worksheet exceeds the configured column limit.")
                        if coordinate in seen_coordinates or int(re.search(r"[0-9]+$", coordinate).group()) != number:
                            raise ExtractionError("parse_failed", "Worksheet contains duplicate or inconsistent cell coordinates.")
                        seen_coordinates.add(coordinate)
                        raw_type = node.get("t", "n")
                        raw = node.findtext("s:v", None, NS)
                        value = raw
                        if raw_type == "s":
                            found = strings.execute("SELECT text FROM strings WHERE id = ?", (int(raw or "-1"),)).fetchone()
                            if found is None:
                                raise ExtractionError("parse_failed", "A shared string reference cannot be resolved.")
                            value = found[0]
                        elif raw_type == "inlineStr":
                            value = _spreadsheet_text(node.find("s:is", NS))
                        cell = {"coordinate": coordinate, "value": value,
                                "value_type": {"n": "number", "s": "string", "inlineStr": "string", "str": "string",
                                               "b": "boolean", "e": "error", "d": "date"}.get(raw_type, raw_type)}
                        style_id = int(node.get("s", "0"))
                        cell["style_id"] = style_id
                        if style_id < len(styles):
                            cell["number_format_id"] = styles[style_id]
                            if styles[style_id] in formats:
                                cell["number_format"] = formats[styles[style_id]]
                        formula = node.find("s:f", NS)
                        if formula is not None:
                            cell.update(formula=formula.text or "", formula_attributes=dict(formula.attrib),
                                        cached_value=raw, cache_verified=False)
                            row_warnings.append("formula_cache_not_verified")
                            if raw is None:
                                row_warnings.append("formula_cache_missing")
                            if formula.get("t") == "shared":
                                row_warnings.append("shared_formula_not_expanded")
                        cells.append(cell)
                    while merge_cursor < len(merge_intervals) and merge_intervals[merge_cursor][0] <= number:
                        _, end, ref = merge_intervals[merge_cursor]
                        if end >= number:
                            active_merges[merge_cursor] = ref
                            heapq.heappush(merge_ends, (end, merge_cursor))
                        merge_cursor += 1
                    while merge_ends and merge_ends[0][0] < number:
                        _, expired = heapq.heappop(merge_ends)
                        active_merges.pop(expired, None)
                    matching_merges = list(active_merges.values())
                    if len(matching_merges) > limits["max_columns"]:
                        raise ExtractionError("limit_exceeded", "Worksheet has too many merged ranges for one row.")
                    row_metadata = dict(metadata, row_hidden=elem.get("hidden") in {"1", "true"},
                                        hidden_columns=hidden_columns, merged_ranges=matching_merges)
                    yield from _row_segments(cells, {"kind": "row", "sheet": sheet.get("name", ""), "row": number},
                                             limits["max_segment_chars"], row_warnings, row_metadata)
                    elem.clear()
                if previous_row == 0:
                    yield from _text_segments(
                        "Worksheet has no extractable cell rows; review format limitations for other content.",
                        {"kind": "worksheet", "sheet": sheet.get("name", "")},
                        limits["max_segment_chars"], warnings + ["empty_worksheet_no_cell_rows"],
                        dict(metadata, cells=[], hidden_columns=hidden_columns, merged_ranges=merged))


def _spreadsheet_text(elem):
    if elem is None:
        return ""
    # Phonetic annotations (rPh) are metadata, not extra cell value text.
    pieces = []
    for child in elem:
        if _local(child.tag) == "t":
            pieces.append(child.text or "")
        elif _local(child.tag) == "r":
            pieces.extend(t.text or "" for t in child.findall("s:t", NS))
    return "".join(pieces)


def _word_text(elem):
    pieces = []
    def collect(node):
        tag = _local(node.tag)
        if tag in {"del", "moveFrom"}:
            return
        if tag == "t":
            pieces.append(node.text or "")
        elif tag == "tab":
            pieces.append("\t")
        elif tag in {"br", "cr"}:
            pieces.append("\n")
        for child in node:
            collect(child)
    collect(elem)
    return "".join(pieces)


def _docx(path, limits):
    warnings = ["word_page_numbers_unavailable", "images_and_embedded_objects_not_extracted",
                "tracked_deletions_excluded", "headers_footers_footnotes_comments_not_extracted"]
    with _Archive(path, limits) as arc:
        if "word/document.xml" not in arc.names:
            raise ExtractionError("parse_failed", "Word main document is missing.")
        table, paragraph, row = 0, 0, 0
        stack = []
        heading = None
        for event, elem in arc.events("word/document.xml"):
            tag = _local(elem.tag)
            if event == "start":
                if not stack and elem.tag != "{" + NS["w"] + "}document":
                    raise ExtractionError("unsupported_structure", "Unsupported Word XML namespace.")
                stack.append(tag)
                if tag == "tbl":
                    if stack.count("tbl") > 1:
                        raise ExtractionError("unsupported_structure", "Nested Word tables require separate review.")
                    table += 1
                    row = 0
                continue
            if tag == "p" and "tbl" not in stack:
                paragraph += 1
                style = elem.find("w:pPr/w:pStyle", NS)
                style_name = style.get("{" + NS["w"] + "}val", "") if style is not None else ""
                text = _word_text(elem)
                if style_name.lower().startswith(("heading", "title")):
                    heading = text[:256]
                yield from _text_segments(text, {"kind": "paragraph", "paragraph": paragraph,
                                                 "heading": heading, "style": style_name},
                                          limits["max_segment_chars"], warnings)
                elem.clear()
            elif tag == "tr":
                row += 1
                cell_nodes = elem.findall("w:tc", NS)
                if len(cell_nodes) > limits["max_columns"]:
                    raise ExtractionError("limit_exceeded", "Word table exceeds the configured column limit.")
                cells = [{"coordinate": _column(index) + str(row), "value": "\n".join(_word_text(p) for p in cell.findall("w:p", NS)),
                          "value_type": "string"} for index, cell in enumerate(cell_nodes, 1)]
                yield from _row_segments(cells, {"kind": "table_row", "table": table, "row": row, "heading": heading},
                                         limits["max_segment_chars"], warnings + ["table_merges_not_expanded"])
                elem.clear()
            if stack:
                stack.pop()


def _drawing_paragraphs(root):
    for paragraph in root.findall(".//a:p", NS):
        text = "".join((node.text or "") if node.tag == "{" + NS["a"] + "}t" else
                       "\n" if node.tag == "{" + NS["a"] + "}br" else "\t"
                       for node in paragraph.iter()
                       if node.tag in {"{" + NS["a"] + "}" + name for name in ("t", "br", "tab")})
        if text:
            yield text


def _pptx(path, limits):
    warnings = ["images_and_charts_not_extracted", "slide_geometry_not_interpreted",
                "smartart_and_embedded_objects_not_extracted"]
    with _Archive(path, limits) as arc:
        presentation = arc.tree("ppt/presentation.xml")
        if presentation.tag != "{" + NS["p"] + "}presentation":
            raise ExtractionError("unsupported_structure", "Unsupported presentation XML namespace.")
        rels = arc.relationships("ppt/presentation.xml")
        for slide_num, slide in enumerate(presentation.findall("p:sldIdLst/p:sldId", NS), 1):
            relation = rels.get(slide.get("{" + NS["r"] + "}id"))
            if not relation:
                raise ExtractionError("parse_failed", "A slide relationship is missing or unsupported.")
            part = relation[0]
            root = arc.tree(part)
            paragraph_num, table_num = 0, 0
            # Read shapes separately from tables to avoid duplicate table text.
            for shape in root.findall(".//p:sp", NS):
                for text in _drawing_paragraphs(shape):
                    paragraph_num += 1
                    yield from _text_segments(text, {"kind": "slide_text", "slide": slide_num,
                                                     "paragraph": paragraph_num}, limits["max_segment_chars"], warnings)
            for table in root.findall(".//a:tbl", NS):
                table_num += 1
                for row_num, row in enumerate(table.findall("a:tr", NS), 1):
                    cells = [{"coordinate": _column(index) + str(row_num), "value": "\n".join(_drawing_paragraphs(cell)),
                              "value_type": "string"} for index, cell in enumerate(row.findall("a:tc", NS), 1)]
                    if len(cells) > limits["max_columns"]:
                        raise ExtractionError("limit_exceeded", "Slide table exceeds the configured column limit.")
                    yield from _row_segments(cells, {"kind": "slide_table_row", "slide": slide_num,
                                                     "table": table_num, "row": row_num},
                                             limits["max_segment_chars"], warnings + ["table_merges_not_expanded"])
            for target, reltype in arc.relationships(part).values():
                if reltype.endswith("/notesSlide"):
                    for note_num, text in enumerate(_drawing_paragraphs(arc.tree(target)), 1):
                        yield from _text_segments(text, {"kind": "slide_notes", "slide": slide_num,
                                                         "paragraph": note_num}, limits["max_segment_chars"], warnings)
            if not paragraph_num and not table_num:
                yield _segment({"kind": "slide", "slide": slide_num}, "", None,
                               warnings + ["no_extractable_slide_text"])


def _pdf(path, limits):
    from pypdf import PdfReader

    # Low-level parser warnings may contain untrusted source fragments. Structured
    # limitations/errors above are the public diagnostic channel.
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    # pypdf may allocate for an entire content stream; the supervisor must apply
    # process memory/time limits in addition to these file/page bounds.
    reader = PdfReader(str(path), strict=True)
    if reader.is_encrypted:
        raise ExtractionError("encrypted", "Encrypted PDF files are not supported.")
    if len(reader.pages) > limits["max_pdf_pages"]:
        raise ExtractionError("limit_exceeded", "PDF exceeds the configured page limit.")
    missing = 0
    for page_num, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        warnings = ["pdf_layout_and_tables_not_verified", "images_not_extracted"]
        if not text.strip():
            missing += 1
            yield _segment({"kind": "page", "page": page_num}, "", None,
                           warnings + ["no_extractable_text_ocr_may_be_required"])
        else:
            yield from _text_segments(text, {"kind": "page", "page": page_num},
                                      limits["max_segment_chars"], warnings)
    # Empty pages remain explicit gap segments. The engine records file-level
    # partial coverage and never presents available text as a complete PDF.


def iter_segments(path: Path, limits: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Yield bounded segments; a later exception means the extraction is partial."""
    path = Path(path)
    config = _limits(limits)
    try:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise ExtractionError("unsupported_format", "This file format is not supported.")
        if not path.is_file():
            raise ExtractionError("source_unavailable", "Source is not an available regular file.")
        if path.stat().st_size > config["max_file_bytes"]:
            raise ExtractionError("limit_exceeded", "File exceeds the configured byte limit.")
        parser = {".txt": _lines, ".md": _lines, ".csv": _csv, ".tsv": _csv,
                  ".xlsx": _xlsx, ".docx": _docx, ".pptx": _pptx, ".pdf": _pdf}[path.suffix.lower()]
        with closing(parser(path, config)) as segments:
            for count, segment in enumerate(segments, 1):
                if count > config["max_segments"]:
                    raise ExtractionError("limit_exceeded", "Extraction exceeds the configured segment limit; results are partial.")
                yield segment
    except ExtractionError:
        raise
    except UnicodeDecodeError:
        raise ExtractionError("unsupported_encoding", "Text must use UTF-8 encoding; convert the file explicitly before retrying.") from None
    except DefusedXmlException:
        raise ExtractionError("unsafe_xml", "DTD, entities, and external XML resources are not allowed.") from None
    except MemoryError:
        raise ExtractionError("limit_exceeded", "Extraction exceeded the available process memory budget.") from None
    except Exception:
        # Parser exceptions can contain paths or source contents. Do not expose
        # them through tool responses or stdout/stderr.
        raise ExtractionError("parse_failed", "The file could not be parsed safely; inspect its format and integrity.") from None


def main():
    try:
        if len(sys.argv) != 3:
            raise ExtractionError("invalid_arguments", "Expected a snapshot path and JSON limits object.")
        limits = json.loads(sys.argv[2])
        if not isinstance(limits, dict):
            raise ExtractionError("invalid_limits", "Limits must be a JSON object.")
        validated = _limits(limits)
        if sys.platform.startswith("linux"):
            # RLIMIT_AS is not consistently enforced on macOS/Windows. Only
            # advertise a hard address-space limit where this backend sets it.
            import resource
            budget = min(2 * 1024**3, max(512 * 1024**2, validated["max_uncompressed_bytes"] * 3))
            current_soft, current_hard = resource.getrlimit(resource.RLIMIT_AS)
            ceiling = budget if current_hard == resource.RLIM_INFINITY else min(budget, current_hard)
            soft = ceiling if current_soft == resource.RLIM_INFINITY else min(ceiling, current_soft)
            resource.setrlimit(resource.RLIMIT_AS, (soft, current_hard))
        for segment in iter_segments(Path(sys.argv[1]), limits):
            print(json.dumps({"segment": segment}, ensure_ascii=False), flush=True)
        print(json.dumps({"done": True}), flush=True)
        return 0
    except ExtractionError as error:
        print(json.dumps({"error": {"code": error.code, "message": error.message}}), flush=True)
        return 1
    except Exception:
        print(json.dumps({"error": {"code": "invalid_arguments", "message": "Arguments could not be read."}}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
