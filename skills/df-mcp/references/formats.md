# Format coverage

Use the result's actual warnings and parser metadata. This table is a conservative interpretation guide, not a promise that every file of a listed extension parses successfully.

| Format | Useful evidence | Important boundaries |
|---|---|---|
| TXT / Markdown | UTF-8 text with line/segment locators | Other encodings must be converted explicitly; very long lines and resource limits can prevent complete extraction. Markdown images and linked documents are not read automatically. |
| CSV / TSV | UTF-8 parsed records and field values with row locators | Quoted newlines can make record numbers differ from physical text lines. CSV uses comma and TSV uses tab; other delimiters need explicit conversion. Verify header, decimal convention and units. These formats provide no inherent field types. |
| XLSX | Sheet/cell values, formula text and saved formula caches where present | No recalculation. Preserve formula and cache warnings. Dates, number formats, hidden content, merged cells, external links, charts, macros and workbook semantics require explicit consideration; blank merged cells must not be invented. |
| DOCX | Body paragraphs and table text with structural locators | No printed page numbers or visual-layout verification. Tracked deletions are excluded; headers, footers, footnotes, comments, images and embedded objects are not extracted. Nested tables are rejected. Do not claim full-document visual coverage. |
| PPTX | Slide text, tables and speaker-note text with slide locators | No visual reconstruction, chart/image interpretation or geometry. Table merges are not expanded. Read the relevant notes when they affect interpretation. |
| PDF | Extracted page text with page locators | No OCR. Pages with no usable text are explicit extraction gaps; available text pages can remain readable as partial evidence. A genuinely blank page is not automatically treated as fully understood. Check the returned extraction completeness and page gaps; an all-unreadable file cannot support text claims. Multi-column order, tables, mathematical notation and image labels can be misread or absent. |
| Other / encrypted / damaged | Metadata or explicit error | Do not claim contents were read. Offer an authorized supported-format export or a separate specialist workflow. |

## Numeric evidence

Inspect the units, currency, dates and definitions next to a number. Do not infer those properties from presentation alone. A spreadsheet formula's stored result may be stale; quote it as a saved value if that is sufficient for the user's request, and request deterministic recalculation when freshness matters.

Do not use default zeros for absent cells, failed conversions, unknown quotes or missing performance results. A percentage or ROI requires its explicit numerator, denominator and reporting window. A source extractor cannot establish causality or business attribution.

## Large documents

Pagination reduces tool output, not necessarily initial download or parse cost. The first read may need the whole source container. Inspect/read/export hash the whole source to validate its version, which is an intentional accuracy cost for large files.

Read structure first, then select relevant units. Do not assume neighboring fragments preserve every table relationship; open adjacent content when necessary. Use controlled export for large exact computations so the model does not need every row in context. Fatal parser failures publish no successful body and clear old content. Explicitly partial extraction (such as readable PDF pages with gaps) is reported separately; never promote it to whole-file coverage. If the runtime rejects a file for resource limits, use an authorized pre-split or supported export; do not silently truncate or bypass the cap.

## What “complete” means

Distinguish: all selected response records returned; all supported text units parsed; all relevant files enumerated; and all business facts needed for the answer available. These are four separate claims. A successfully parsed DOCX/PDF/PPTX does not guarantee that images or visual relationships were understood.
