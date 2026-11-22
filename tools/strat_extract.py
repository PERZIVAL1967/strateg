"""Extractors for strat (plan v1.3 §11, D01-D03). Pure functions: bytes in, segments out.

A segment is one addressable piece of text:
  {"idx": n, "kind": "paragraph|heading|cell|page", "hint": structural address,
   "text": str, "context": str}
`hint` is a structural hint only (paragraph/table/cell/page/sheet address); the primary locator is
a text quote resolved against these segments (strat locate). `context` is what must stay the same
for a carried-forward claim to keep its meaning: the heading above a paragraph, or the column
header + row label of a cell, plus units if the source states them.

Each extractor returns (segments, extraction, coverage):
  extraction: "full" | "partial" | "blocked:<reason>"
  coverage:   dict of what was and wasn't read
Values are never invented: formulas without a cached value, ambiguous dates and missing units
stay as raw text with a flag.
"""
import csv
import html.parser
import io
import re
import zipfile
import xml.etree.ElementTree as ET

EXTRACTION_VERSION = "seg-2"   # seg-2: HTML table cells carry table/header/row context
MAX_ZIP_MEMBERS = 2000
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
PDF_LIB_VERSION = None
try:  # optional, pinned in requirements-optional.txt
    import pypdf
    PDF_LIB_VERSION = pypdf.__version__
except ImportError:
    pypdf = None


def _seg(out, kind, hint, text, context=""):
    text = re.sub(r"[ \t\r\f\v]+", " ", text).strip()
    if text:
        out.append({"idx": len(out), "kind": kind, "hint": hint, "text": text, "context": context})


def detect_kind(name, data, content_type=""):
    n = name.lower()
    ct = (content_type or "").lower()
    if n.endswith(".pdf") or data[:5] == b"%PDF-" or "pdf" in ct:
        return "pdf"
    if n.endswith(".docx"):
        return "docx"
    if n.endswith(".xlsx"):
        return "xlsx"
    if n.endswith(".csv") or "csv" in ct:
        return "csv"
    if n.endswith((".html", ".htm")) or "html" in ct or data.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    if n.endswith((".txt", ".md", ".markdown")) or ct.startswith("text/"):
        return "text"
    return "unknown"


def extract(name, data, content_type=""):
    kind = detect_kind(name, data, content_type)
    fn = {"text": extract_text, "csv": extract_csv, "html": extract_html, "docx": extract_docx,
          "xlsx": extract_xlsx, "pdf": extract_pdf}.get(kind)
    if fn is None:
        return [], "blocked:unsupported_format", {"detected": kind}
    segs, extraction, coverage = fn(data)
    coverage["format"] = kind
    return segs, extraction, coverage


def _decode(data):
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def extract_text(data):
    text, enc = _decode(data)
    out, heading = [], ""
    for i, block in enumerate(re.split(r"\n\s*\n", text)):
        line = block.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            _seg(out, "heading", f"p{i}", heading)
        else:
            _seg(out, "paragraph", f"p{i}", line, heading)
    return out, "full", {"encoding": enc, "segments": len(out)}


def extract_csv(data):
    text, enc = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    out = []
    header = rows[0] if rows else []
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            col = header[c] if c < len(header) else f"col{c}"
            label = row[0] if row else ""
            ctx = f"column: {col}" if r == 0 else f"column: {col} | row: {label}"
            _seg(out, "cell", f"r{r}c{c}", val, ctx)
    return out, "full", {"encoding": enc, "rows": len(rows), "delimiter": dialect.delimiter,
                         "units": "unknown unless stated in a header"}


class _HTML(html.parser.HTMLParser):
    """Server-rendered HTML -> segments. Table cells carry table identity, column header and row
    label, so the same number under a different header gets a different context (plan v1.3 §11)."""
    BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "caption", "dt", "dd", "blockquote", "pre"}
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.buf, self.heading = [], [], ""
        self.block = None
        self.skip = 0
        self.scripts = 0
        self.counts = {}
        self.tables = []          # stack of open tables
        self.table_no = 0
        self.unsupported = []     # tables whose structure we can't map to headers (rowspan)

    # -- tables
    def _t(self):
        return self.tables[-1] if self.tables else None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
            self.scripts += tag == "script"
        elif tag == "table":
            self._flush_block()
            self.tables.append({"no": self.table_no, "rows": [], "row": None, "cell": None,
                                "caption": "", "rowspan": False})
            self.table_no += 1
        elif tag == "tr" and self._t():
            self._t()["row"] = []
        elif tag in ("td", "th") and self._t() and self._t()["row"] is not None:
            try:
                span = max(1, int(a.get("colspan", "1")))
            except ValueError:
                span = 1
            if a.get("rowspan", "1") not in ("", "1"):
                self._t()["rowspan"] = True
            self._t()["cell"] = {"tag": tag, "text": [], "span": span}
        elif tag in self.BLOCK:
            self._flush_block()
            self.block = tag

    def handle_endtag(self, tag):
        t = self._t()
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th") and t and t["cell"] is not None:
            c = t["cell"]
            t["row"].append((c["tag"], re.sub(r"\s+", " ", "".join(c["text"])).strip(), c["span"]))
            t["cell"] = None
        elif tag == "tr" and t and t["row"] is not None:
            t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table" and t:
            self.tables.pop()
            self._emit_table(t)
        elif tag == "caption" and t:
            t["caption"] = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            self.buf = []
            self.block = None
        elif tag in self.BLOCK:
            self._flush_block(tag)

    def handle_data(self, d):
        if self.skip:
            return
        t = self._t()
        if t and t["cell"] is not None:
            t["cell"]["text"].append(d)
        else:
            self.buf.append(d)

    def _flush_block(self, tag=None):
        text = "".join(self.buf).strip()
        self.buf = []
        tag = tag or self.block or "text"
        self.block = None
        if not text:
            return
        n = self.counts.get(tag, 0)
        self.counts[tag] = n + 1
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = text
            _seg(self.out, "heading", f"{tag}[{n}]", text)
        else:
            _seg(self.out, "paragraph", f"{tag}[{n}]", text, self.heading)

    def _emit_table(self, t):
        if t["rowspan"]:
            self.unsupported.append(t["no"])
        # expand colspans into grid columns; remember where each cell starts and how wide it is
        grid = []
        for row in t["rows"]:
            cols = []
            for tag, text, span in row:
                cols.extend([(tag, text, span if k == 0 else 0) for k in range(span)])
            grid.append(cols)
        header = []
        if grid and grid[0] and all(tag == "th" for tag, _, _ in grid[0]):
            header = [text for _, text, _ in grid[0]]
        base = " | ".join(x for x in (self.heading, f"table {t['no']}", t["caption"] and f"caption: {t['caption']}") if x)
        for r, cols in enumerate(grid):
            for c, (tag, text, span) in enumerate(cols):
                if span == 0:      # covered by a colspan cell to its left, already emitted
                    continue
                names = [header[i] if i < len(header) else f"col{i}" for i in range(c, c + span)]
                col = " / ".join(dict.fromkeys(names))
                label = cols[0][1] if c > 0 else ""
                ctx = base + f" | column: {col}" + (f" | row: {label}" if label and r > 0 else "")
                if t["rowspan"]:
                    ctx += " | structure: rowspan (row/column mapping not verified)"
                _seg(self.out, "cell", f"table{t['no']}!r{r}c{c}" + (f"+{span - 1}" if span > 1 else ""), text, ctx)


def extract_html(data):
    text, enc = _decode(data)
    p = _HTML()
    p.feed(text)
    p._flush_block()
    chars = sum(len(s["text"]) for s in p.out)
    if chars < 200 and p.scripts:
        # server sent a JS shell: we don't run scripts, so the content isn't here (D06)
        return p.out, "blocked:dynamic", {"visible_chars": chars, "scripts": p.scripts}
    cov = {"encoding": enc, "segments": len(p.out), "tables": p.table_no, "server_rendered_only": True}
    if p.unsupported:
        cov["tables_with_rowspan"] = p.unsupported
        return p.out, "partial", cov
    return p.out, "full", cov


def _open_zip(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    infos = z.infolist()
    if len(infos) > MAX_ZIP_MEMBERS or sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise ValueError("archive too large when unpacked")
    for i in infos:
        if i.filename.startswith("/") or ".." in i.filename.split("/"):
            raise ValueError(f"unsafe member path {i.filename!r}")
    return z


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx(data):
    try:
        z = _open_zip(data)
        root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError) as e:
        return [], "blocked:unreadable", {"error": str(e)}
    out, heading, pn, tn = [], "", 0, 0
    body = root.find(f"{W}body")
    for el in list(body) if body is not None else []:
        if el.tag == f"{W}p":
            text = "".join(t.text or "" for t in el.iter(f"{W}t"))
            style = el.find(f"{W}pPr/{W}pStyle")
            sval = style.get(f"{W}val", "") if style is not None else ""
            if re.search(r"heading|заголов", sval, re.I) and text.strip():
                heading = text.strip()
                _seg(out, "heading", f"p{pn}", text)
            else:
                _seg(out, "paragraph", f"p{pn}", text, heading)
            pn += 1
        elif el.tag == f"{W}tbl":
            rows = el.findall(f"{W}tr")
            cells = [["".join(t.text or "" for t in c.iter(f"{W}t")) for c in r.findall(f"{W}tc")] for r in rows]
            header = cells[0] if cells else []
            for r, row in enumerate(cells):
                for c, val in enumerate(row):
                    col = header[c] if c < len(header) else f"col{c}"
                    ctx = f"{heading} | table {tn} | column: {col}" + (f" | row: {row[0]}" if r and row else "")
                    _seg(out, "cell", f"t{tn}r{r}c{c}", val, ctx)
            tn += 1
    return out, "full", {"paragraphs": pn, "tables": tn}


S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_row(ref):
    m = re.fullmatch(r"([A-Z]+)(\d+)", ref)
    return (m.group(1), int(m.group(2))) if m else (ref, 0)


def extract_xlsx(data):
    try:
        z = _open_zip(data)
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(f"{S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{S}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    except (zipfile.BadZipFile, KeyError, ValueError, ET.ParseError) as e:
        return [], "blocked:unreadable", {"error": str(e)}
    target = {r.get("Id"): r.get("Target") for r in rels}
    out, formulas_without_value, merged = [], [], 0
    for sheet in wb.iter(f"{S}sheet"):
        sname = sheet.get("name")
        path = target.get(sheet.get(f"{R_NS}id"), "")
        path = path.lstrip("/") if path.startswith("/") else "xl/" + path
        if path not in names:
            continue
        root = ET.fromstring(z.read(path))
        grid = {}
        for c in root.iter(f"{S}c"):
            ref, t = c.get("r"), c.get("t")
            v, f = c.find(f"{S}v"), c.find(f"{S}f")
            if v is None and f is not None:
                formulas_without_value.append(f"{sname}!{ref}")
                grid[ref] = ("", "formula_without_cached_value")
                continue
            if v is None:
                is_ = c.find(f"{S}is")
                grid[ref] = ("".join(x.text or "" for x in is_.iter(f"{S}t")) if is_ is not None else "", None)
                continue
            val = v.text or ""
            if t == "s":
                val = shared[int(val)] if val.isdigit() and int(val) < len(shared) else val
            grid[ref] = (val, None)   # numbers kept raw: date/number formats are not interpreted (D03)
        merges = {}
        for m in root.iter(f"{S}mergeCell"):
            rng = m.get("ref", "")
            merges[rng.split(":")[0]] = rng
            merged += 1
        header = {_col_row(r)[0]: v for r, (v, _) in grid.items() if _col_row(r)[1] == 1}
        first_col = {_col_row(r)[1]: v for r, (v, _) in grid.items() if _col_row(r)[0] == "A"}
        for ref in sorted(grid, key=lambda r: (_col_row(r)[1], len(_col_row(r)[0]), _col_row(r)[0])):
            val, flag = grid[ref]
            col, row = _col_row(ref)
            hint = f"{sname}!{ref}" + (f" (merged {merges[ref]})" if ref in merges else "")
            ctx = f"sheet: {sname} | column: {header.get(col, col)}" + (f" | row: {first_col.get(row, row)}" if row > 1 else "")
            if flag:
                out.append({"idx": len(out), "kind": "cell", "hint": hint, "text": "", "context": ctx, "flag": flag})
            else:
                _seg(out, "cell", hint, val, ctx)
    cov = {"formulas_without_cached_value": formulas_without_value, "merged_ranges": merged,
           "number_formats": "not interpreted; values are raw"}
    return out, "partial" if formulas_without_value else "full", cov


def extract_pdf(data):
    if pypdf is None:
        return [], "blocked:dependency_missing", {"needs": "pypdf (see requirements-optional.txt)"}
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = reader.pages
        out, with_text, empty = [], 0, []
        for i, page in enumerate(pages):
            text = page.extract_text() or ""
            if text.strip():
                with_text += 1
                for j, para in enumerate(re.split(r"\n\s*\n", text)):
                    _seg(out, "paragraph", f"page{i + 1}.p{j}", para.replace("\n", " "), f"page {i + 1}")
            else:
                empty.append(i + 1)
    except Exception as e:  # malformed PDFs raise many types; report, never guess
        return [], "blocked:unreadable", {"error": f"{type(e).__name__}: {e}"}
    cov = {"pages_total": len(pages), "pages_with_text": with_text, "pages_without_text": empty,
           "pypdf": PDF_LIB_VERSION, "ocr": "not performed"}
    if with_text == 0:
        return out, "blocked:scanned", cov
    return out, "partial" if empty else "full", cov


def locate(segments, exact, prefix="", suffix="", hint=None):
    """Text-quote selector: return the unique match or say why there isn't one. Never guess."""
    matches = []
    for s in segments:
        if hint and s["hint"] != hint:
            continue
        text = s["text"]
        start = text.find(exact)
        while start != -1:
            before, after = text[:start], text[start + len(exact):]
            if before.endswith(prefix) and after.startswith(suffix):
                matches.append((s, start))
            start = text.find(exact, start + 1)
    if len(matches) == 1:
        s, start = matches[0]
        return {"resolved": True, "segment_idx": s["idx"], "hint": s["hint"], "offset": start, "segment": s}
    return {"resolved": False, "reason": "not_found" if not matches else "ambiguous",
            "candidates": [{"segment_idx": s["idx"], "hint": s["hint"]} for s, _ in matches][:20]}
