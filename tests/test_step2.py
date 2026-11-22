"""Build step 2 gate: fetch with network guards, extractors, quote locator, exports (plan v1.3 §11).

Run: python -m unittest discover -s tests -v   (from the agent folder; macOS, Linux, Windows)
Fixtures are built here, so the suite needs no files from outside. A local HTTP server on
127.0.0.1 stands in for the web; STRAT_TEST_ALLOW_LOOPBACK=1 unlocks exactly that address.
"""
import http.server
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile

STRAT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "strat.py"
LOOP = {"STRAT_TEST_ALLOW_LOOPBACK": "1"}
try:
    import pypdf  # noqa: F401
    HAVE_PDF = True
except ImportError:
    HAVE_PDF = False


def run(root, *args, env=None):
    """Run strat; its stdout is UTF-8 JSON by contract, whatever the console encoding is."""
    e = {**os.environ, **(env or {})}
    p = subprocess.run([sys.executable, str(STRAT), "--root", root, *args], capture_output=True, env=e)
    try:
        text = p.stdout.decode("utf-8")
    except UnicodeDecodeError as err:
        raise AssertionError(f"strat stdout is not UTF-8 ({err}); stderr: {p.stderr.decode('utf-8', 'replace')}")
    out = json.loads(text) if text.strip() else None
    return p.returncode, out


# ---------------------------------------------------------------- fixture builders

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path):
    def p(text, style=None):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"

    def row(*cells):
        return "<w:tr>" + "".join(f"<w:tc>{p(c)}</w:tc>" for c in cells) + "</w:tr>"
    body = (p("Тарифы связи", "Heading1") + p("Абонентская плата составляет 500 руб. в месяц.")
            + "<w:tbl>" + row("Тариф", "Цена, руб.") + row("Базовый", "500") + row("Плюс", "900") + "</w:tbl>")
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)


S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def make_xlsx(path):
    shared = ["Месяц", "Пользователи", "Итого"]
    sst = f'<sst xmlns="{S_NS}">' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>"
    wb = (f'<workbook xmlns="{S_NS}" xmlns:r="{R_NS}"><sheets>'
          '<sheet name="Данные" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>')
    sheet = (f'<worksheet xmlns="{S_NS}"><sheetData>'
             '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
             '<row r="2"><c r="A2" t="inlineStr"><is><t>2026-09</t></is></c><c r="B2"><v>120</v></c></row>'
             '<row r="3"><c r="A3" t="inlineStr"><is><t>2026-10</t></is></c><c r="B3"><v>135</v></c></row>'
             '<row r="4"><c r="A4" t="s"><v>2</v></c><c r="B4"><f>SUM(B2:B3)</f></c></row>'
             '</sheetData><mergeCells count="1"><mergeCell ref="A5:B5"/></mergeCells></worksheet>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/sharedStrings.xml", sst)
        z.writestr("xl/worksheets/sheet1.xml", sheet)


def make_pdf(path, page_texts):
    """Minimal valid PDF; an empty string makes a page with no text layer (like a scan)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in page_texts:
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objs.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
        cid = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cid} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        body = o if isinstance(o, bytes) else o.encode()
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    pathlib.Path(path).write_bytes(bytes(out))


HTML_PAGE = """<!doctype html><html><head><title>Тарифы</title><script>var t=1;</script></head><body>
<h2>Мобильная связь</h2><p>Тариф «Базовый» стоит 500 руб. в месяц и включает 10 ГБ.</p>
<p>Тариф «Плюс» стоит 900 руб. в месяц и включает 30 ГБ.</p>
<table><tr><th>Тариф</th><th>ГБ</th></tr><tr><td>Базовый</td><td>10</td></tr></table>
<p>Подробности уточняйте у оператора. Подробности уточняйте у оператора.</p></body></html>"""
HTML_DYNAMIC = '<!doctype html><html><body><div id="root"></div><script src="/app.js"></script><script>boot()</script></body></html>'


class Server:
    """127.0.0.1 HTTP server: files from a dict, plus /redirect-private and request counting."""

    def __init__(self):
        self.files, self.hits = {}, {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.hits[self.path] = outer.hits.get(self.path, 0) + 1
                if self.path == "/redirect-private":
                    self.send_response(302)
                    self.send_header("Location", "http://10.0.0.1/secret")
                    self.end_headers()
                    return
                if self.path == "/trickle":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    try:
                        for _ in range(100):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.2)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if self.path.startswith("/slow-hop/"):
                    n = int(self.path.rsplit("/", 1)[1])
                    time.sleep(0.6 if n > 0 else 5)       # last hop stalls before sending headers
                    if n > 0:
                        self.send_response(302)
                        self.send_header("Location", f"http://127.0.0.1:{outer.port}/slow-hop/{n - 1}")
                        self.end_headers()
                    else:
                        try:
                            self.send_response(200)
                            self.end_headers()
                            self.wfile.write(b"late")
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    return
                if self.path == "/redirect-local":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/page.html")
                    self.end_headers()
                    return
                body, ctype = outer.files.get(self.path, (None, None))
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.assertEqual(run(self.root, "init", "--project", "demo")[0], 0)
        self.token = run(self.root, "lease", "--project", "demo", "--session", "chatA")[1]["data"]["fencing_token"]
        self.step = 0

    def allow_hosts(self):
        """Step 4 made fetch allowlist-only; allow the test hosts so the address checks are what's tested."""
        for i, h in enumerate(("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "127.0.0.2")):
            code, out = run(self.root, "allow", "--project", "demo", "--session", "chatA", "--token", str(self.token),
                            "--run", "policy", "--step", str(i + 1), "--pattern", h, "--covers", "host",
                            "--reason", "test host")
            self.assertEqual(code, 0, out)

    def acquire(self, cmd, target, env=None, step=None):
        self.step += 1
        flag = "--file" if cmd == "import" else "--url"
        return run(self.root, cmd, "--project", "demo", "--session", "chatA", "--token", str(self.token),
                   "--run", "r1", "--step", str(step or self.step), flag, str(target), env=env)

    def extract(self, sv):
        return run(self.root, "extract", "--project", "demo", "--session", "chatA", "--token", str(self.token),
                   "--source-version", sv)

    def locate(self, sv, exact, **kw):
        args = ["locate", "--project", "demo", "--source-version", sv, "--exact", exact]
        for k, v in kw.items():
            args += [f"--{k}", v]
        return run(self.root, *args)[1]["data"]

    def import_and_extract(self, path):
        code, out = self.acquire("import", path)
        self.assertEqual(code, 0, out)
        sv = out["data"]["source_version_id"]
        code, ex = self.extract(sv)
        self.assertEqual(code, 0, ex)
        return sv, ex["data"]


class Extractors(Base):
    def test_text_markdown_headings_give_context(self):
        f = pathlib.Path(self.root, "notes.md")
        f.write_text("# Рынок\n\nДоля мобильного интернета растёт.\n\n# Риски\n\nРегулятор меняет правила.\n", encoding="utf-8")
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "full")
        r = self.locate(sv, "Регулятор меняет")
        self.assertTrue(r["resolved"])
        self.assertEqual(r["context"], "Риски")

    def test_csv_cells_carry_header_and_row(self):
        f = pathlib.Path(self.root, "users.csv")
        f.write_text("month;users\n2026-09;120\n2026-10;135\n", encoding="utf-8")
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["coverage"]["delimiter"], ";")
        r = self.locate(sv, "135")
        self.assertTrue(r["resolved"])
        self.assertEqual(r["context"], "column: users | row: 2026-10")

    def test_docx_paragraphs_and_table(self):
        f = pathlib.Path(self.root, "tariffs.docx")
        make_docx(f)
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["coverage"]["tables"], 1)
        r = self.locate(sv, "500 руб.")
        self.assertTrue(r["resolved"])
        self.assertEqual(r["context"], "Тарифы связи")
        cell = self.locate(sv, "900")
        self.assertTrue(cell["resolved"])
        self.assertIn("column: Цена, руб.", cell["context"])
        self.assertIn("row: Плюс", cell["context"])

    def test_xlsx_formula_without_value_is_unknown_not_invented(self):
        f = pathlib.Path(self.root, "data.xlsx")
        make_xlsx(f)
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "partial")
        self.assertEqual(ex["coverage"]["formulas_without_cached_value"], ["Данные!B4"])
        self.assertEqual(ex["coverage"]["merged_ranges"], 1)
        self.assertFalse(self.locate(sv, "255")["resolved"], "SUM is never computed")
        r = self.locate(sv, "135")
        self.assertIn("column: Пользователи", r["context"])
        self.assertIn("row: 2026-10", r["context"])

    def test_html_server_rendered(self):
        f = pathlib.Path(self.root, "page.html")
        f.write_text(HTML_PAGE, encoding="utf-8")
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "full")
        r = self.locate(sv, "900 руб.")
        self.assertTrue(r["resolved"])
        self.assertEqual(r["context"], "Мобильная связь")

    def test_html_js_shell_is_blocked_dynamic(self):
        f = pathlib.Path(self.root, "app.html")
        f.write_text(HTML_DYNAMIC, encoding="utf-8")
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "blocked:dynamic")

    def test_unknown_format_is_blocked(self):
        f = pathlib.Path(self.root, "blob.bin")
        f.write_bytes(b"\x00\x01\x02binary")
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "blocked:unsupported_format")

    def test_zip_path_traversal_is_refused(self):
        f = pathlib.Path(self.root, "evil.docx")
        with zipfile.ZipFile(f, "w") as z:
            z.writestr("../../escape.txt", "x")
            z.writestr("word/document.xml", "<x/>")
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "blocked:unreadable")

    @unittest.skipUnless(HAVE_PDF, "pypdf not installed (optional dependency)")
    def test_pdf_page_coverage_and_scan_page(self):
        f = pathlib.Path(self.root, "report.pdf")
        make_pdf(f, ["Market share grew to 41 percent", ""])
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "partial", "one page has no text layer")
        self.assertEqual(ex["coverage"]["pages_without_text"], [2])
        r = self.locate(sv, "41 percent")
        self.assertTrue(r["resolved"])
        self.assertEqual(r["hint"].split(".")[0], "page1")

    @unittest.skipUnless(HAVE_PDF, "pypdf not installed (optional dependency)")
    def test_pdf_all_pages_scanned_is_blocked(self):
        f = pathlib.Path(self.root, "scan.pdf")
        make_pdf(f, ["", ""])
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "blocked:scanned")

    def test_extract_is_idempotent(self):
        f = pathlib.Path(self.root, "notes.md")
        f.write_text("Один абзац.\n", encoding="utf-8")
        sv, ex = self.import_and_extract(f)
        code, again = self.extract(sv)
        self.assertEqual(again["status"], "skipped")


class Locator(Base):
    def setUp(self):
        super().setUp()
        f = pathlib.Path(self.root, "page.html")
        f.write_text(HTML_PAGE, encoding="utf-8")
        self.sv, _ = self.import_and_extract(f)

    def test_unique_quote_resolves_with_hashes(self):
        r = self.locate(self.sv, "30 ГБ")
        self.assertTrue(r["resolved"])
        self.assertEqual(len(r["span_hash"]), 64)
        self.assertEqual(len(r["context_hash"]), 64)

    def test_repeated_phrase_is_ambiguous_not_guessed(self):
        r = self.locate(self.sv, "стоит")
        self.assertFalse(r["resolved"])
        self.assertEqual(r["reason"], "ambiguous")
        self.assertGreaterEqual(len(r["candidates"]), 2)

    def test_prefix_suffix_disambiguate(self):
        r = self.locate(self.sv, "стоит", prefix="«Плюс» ", suffix=" 900")
        self.assertTrue(r["resolved"])

    def test_same_phrase_twice_in_one_segment_is_ambiguous(self):
        r = self.locate(self.sv, "Подробности уточняйте у оператора")
        self.assertEqual(r["reason"], "ambiguous")

    def test_hint_disambiguates(self):
        r = self.locate(self.sv, "Базовый", hint="table0!r1c0")
        self.assertTrue(r["resolved"])

    def test_missing_quote_is_not_found(self):
        r = self.locate(self.sv, "бесплатно навсегда")
        self.assertEqual((r["resolved"], r["reason"]), (False, "not_found"))


class Fetch(Base):
    def setUp(self):
        super().setUp()
        self.srv = Server()
        self.addCleanup(self.srv.close)
        self.srv.files["/page.html"] = (HTML_PAGE.encode(), "text/html; charset=utf-8")
        self.allow_hosts()

    def test_fetch_then_extract(self):
        code, out = self.acquire("fetch", self.srv.url("/page.html"), env=LOOP)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["data"]["http_status"], 200)
        self.assertIn("text/html", out["data"]["content_type"])
        code, ex = self.extract(out["data"]["source_version_id"])
        self.assertEqual(ex["data"]["status"], "full")

    def test_loopback_blocked_without_test_flag(self):
        code, out = self.acquire("fetch", self.srv.url("/page.html"))
        self.assertEqual((out["error_code"], out["data"]["reason"]), ("BLOCKED", "private_address"))
        self.assertEqual(self.srv.hits, {}, "no request was sent")

    def test_private_ranges_blocked_even_with_test_flag(self):
        for url in ("http://10.0.0.1/", "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data",
                    "http://[::1]/", "http://127.0.0.2/"):
            code, out = self.acquire("fetch", url, env=LOOP)
            self.assertEqual(out["error_code"], "BLOCKED", url)

    def test_scheme_and_credentials_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "http://user:pw@example.com/"):
            code, out = self.acquire("fetch", url, env=LOOP)
            self.assertEqual(out["error_code"], "BLOCKED", url)

    def test_redirect_to_private_address_blocked(self):
        code, out = self.acquire("fetch", self.srv.url("/redirect-private"), env=LOOP)
        self.assertEqual((out["error_code"], out["data"]["reason"]), ("BLOCKED", "private_address"))

    def test_redirect_records_final_url(self):
        code, out = self.acquire("fetch", self.srv.url("/redirect-local"), env=LOOP)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["data"]["final_url"].endswith("/page.html"))

    def test_404_is_failed_attempt_then_retry_succeeds(self):
        url = self.srv.url("/later.html")
        code, out = self.acquire("fetch", url, env=LOOP, step="9")
        self.assertEqual(out["error_code"], "NOT_FOUND")
        self.srv.files["/later.html"] = (HTML_PAGE.encode(), "text/html")
        code, out = self.acquire("fetch", url, env=LOOP, step="9")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["data"]["attempt_no"], 2)
        st = run(self.root, "status", "--project", "demo")[1]["data"]
        fetch_ops = {o["op_id"] for o in st["operations"] if o["tool"] == "fetch"}
        self.assertEqual([(a["outcome"], a["error_code"]) for a in st["attempts"] if a["op_id"] in fetch_ops],
                         [("failed", "NOT_FOUND"), ("ok", None)])

    def test_crash_after_receipt_does_not_refetch(self):
        url = self.srv.url("/page.html")
        code, _ = self.acquire("fetch", url, env={**LOOP, "STRAT_CRASH_AT": "after_receipt"}, step="5")
        self.assertEqual(code, 137)
        code, out = self.acquire("fetch", url, env=LOOP, step="5")
        self.assertEqual(code, 0, out)
        self.assertTrue(out["data"]["resumed"])
        self.assertEqual(self.srv.hits["/page.html"], 1, "the page was downloaded exactly once")

    def test_same_step_refetch_is_skipped_new_step_is_new_version(self):
        url = self.srv.url("/page.html")
        self.acquire("fetch", url, env=LOOP, step="7")
        code, out = self.acquire("fetch", url, env=LOOP, step="7")
        self.assertEqual(out["status"], "skipped")
        self.srv.files["/page.html"] = (HTML_PAGE.replace("900", "950").encode(), "text/html")
        code, out = self.acquire("fetch", url, env=LOOP, step="8")
        self.assertEqual(out["status"], "ok")
        st = run(self.root, "status", "--project", "demo")[1]["data"]
        self.assertEqual(len(st["source_versions"]), 2)
        self.assertEqual(len({s["source_id"] for s in st["source_versions"]}), 1, "one source, two versions")


class Step2Fixes(Base):
    """Regressions for council #3 R027 (Windows run of step 2)."""

    def test_output_is_utf8_under_a_legacy_console_encoding(self):
        legacy = {"PYTHONIOENCODING": "cp1252"}   # a code page with no Cyrillic, like many Windows consoles
        f = pathlib.Path(self.root, "заметки.md")
        f.write_text("# Тарифы\n\nБазовый тариф - 500 руб.\n", encoding="utf-8")
        code, out = self.acquire("import", f, env=legacy)
        self.assertEqual(code, 0, out)
        sv = out["data"]["source_version_id"]
        code, ex = run(self.root, "extract", "--project", "demo", "--session", "chatA", "--token", str(self.token),
                       "--source-version", sv, env=legacy)
        self.assertEqual(code, 0, ex)
        r = run(self.root, "locate", "--project", "demo", "--source-version", sv, "--exact", "500 руб.", env=legacy)[1]["data"]
        self.assertEqual(r["context"], "Тарифы", "Cyrillic survives a non-Cyrillic console encoding")

    def test_same_number_under_changed_header_changes_context(self):
        hashes = []
        for header in ("Пользователи в месяц", "Пользователи в год"):
            self.step += 1
            f = pathlib.Path(self.root, f"t{self.step}.html")
            f.write_text(f"<html><body><p>{'Отчёт оператора. ' * 20}</p><table><tr><th>Период</th><th>{header}</th></tr>"
                         f"<tr><td>2026</td><td>120</td></tr></table></body></html>", encoding="utf-8")
            sv, _ = self.import_and_extract(f)
            r = self.locate(sv, "120")
            self.assertTrue(r["resolved"])
            self.assertIn(f"column: {header}", r["context"])
            self.assertIn("row: 2026", r["context"])
            hashes.append((r["span_hash"], r["context_hash"]))
        self.assertEqual(hashes[0][0], hashes[1][0], "same quoted span")
        self.assertNotEqual(hashes[0][1], hashes[1][1], "different meaning -> different context hash")

    def test_colspan_cell_emitted_once_with_both_headers(self):
        f = pathlib.Path(self.root, "span.html")
        f.write_text("<html><body><p>" + "Тарифы. " * 30 + "</p><table><tr><th>Тариф</th><th>Цена</th><th>ГБ</th></tr>"
                     "<tr><td>Плюс</td><td colspan=\"2\">по запросу</td></tr></table></body></html>", encoding="utf-8")
        sv, ex = self.import_and_extract(f)
        r = self.locate(sv, "по запросу")
        self.assertTrue(r["resolved"], "one segment, not one per spanned column")
        self.assertIn("column: Цена / ГБ", r["context"])

    def test_rowspan_table_is_partial_not_full(self):
        f = pathlib.Path(self.root, "rows.html")
        f.write_text("<html><body><p>" + "Данные. " * 30 + "</p><table><tr><th>A</th><th>B</th></tr>"
                     "<tr><td rowspan=\"2\">x</td><td>1</td></tr><tr><td>2</td></tr></table></body></html>", encoding="utf-8")
        sv, ex = self.import_and_extract(f)
        self.assertEqual(ex["status"], "partial")
        self.assertEqual(ex["coverage"]["tables_with_rowspan"], [0])


class FetchDeadline(Base):
    def setUp(self):
        super().setUp()
        self.srv = Server()
        self.addCleanup(self.srv.close)
        self.srv.files["/page.html"] = (HTML_PAGE.encode(), "text/html; charset=utf-8")
        self.allow_hosts()

    def test_trickle_server_hits_the_total_deadline(self):
        t0 = time.monotonic()
        code, out = self.acquire("fetch", self.srv.url("/trickle"), env={**LOOP, "STRAT_FETCH_TIMEOUT_S": "1.5"})
        elapsed = time.monotonic() - t0
        self.assertEqual(out["error_code"], "TIMEOUT", out)
        self.assertLess(elapsed, 6, "the whole download is bounded, not each read")

    def test_slow_redirect_chain_shares_one_budget(self):
        # two 0.6 s redirects, then a hop that stalls 5 s before headers; budget 2 s.
        # Without passing the remaining budget on, the last hop would get the full 2 s again (~3.2 s).
        t0 = time.monotonic()
        code, out = self.acquire("fetch", self.srv.url("/slow-hop/2"), env={**LOOP, "STRAT_FETCH_TIMEOUT_S": "2"})
        elapsed = time.monotonic() - t0
        self.assertEqual(out["error_code"], "TIMEOUT", out)
        self.assertLess(elapsed, 2.9, f"took {elapsed:.2f}s - the redirect chain must share one budget")

    def test_environment_proxy_is_ignored(self):
        code, out = self.acquire("fetch", self.srv.url("/page.html"),
                                 env={**LOOP, "HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9"})
        self.assertEqual(code, 0, out)


class Exports(Base):
    def test_export_manifest_detects_edits_and_staleness(self):
        f = pathlib.Path(self.root, "notes.md")
        f.write_text("Один абзац.\n", encoding="utf-8")
        sv, _ = self.import_and_extract(f)
        code, out = run(self.root, "export", "--project", "demo")
        self.assertEqual(code, 0, out)
        exp = pathlib.Path(self.root, "projects/demo/exports")
        self.assertTrue((exp / "sources.md").read_text(encoding="utf-8").startswith("# Источники"))
        self.assertTrue((exp / "dump/source_version.jsonl").exists())
        d = run(self.root, "doctor", "--project", "demo")[1]["data"]["exports"]
        self.assertEqual(d["state"], "current")
        (exp / "sources.md").write_text("# Источники\n\nподправлено вручную\n", encoding="utf-8")
        d = run(self.root, "doctor", "--project", "demo")[1]["data"]["exports"]
        self.assertEqual((d["state"], d["edited_files"]), ("edited", ["sources.md"]))
        run(self.root, "export", "--project", "demo")
        g = pathlib.Path(self.root, "more.md")
        g.write_text("Ещё.\n", encoding="utf-8")
        self.acquire("import", g)
        d = run(self.root, "doctor", "--project", "demo")[1]["data"]["exports"]
        self.assertEqual(d["state"], "stale", "DB moved on since the last export")

    def test_dump_includes_segments(self):
        f = pathlib.Path(self.root, "notes.md")
        f.write_text("Один абзац.\n", encoding="utf-8")
        self.import_and_extract(f)
        code, out = run(self.root, "export", "--project", "demo")
        self.assertEqual(out["data"]["kind"], "view_export")
        seg = pathlib.Path(self.root, "projects/demo/exports/dump/segment.jsonl").read_text(encoding="utf-8")
        self.assertIn("Один абзац.", seg)

    def test_crash_between_files_and_manifest_is_never_current(self):
        f = pathlib.Path(self.root, "notes.md")
        f.write_text("Один абзац.\n", encoding="utf-8")
        self.import_and_extract(f)
        run(self.root, "export", "--project", "demo")
        g = pathlib.Path(self.root, "more.md")
        g.write_text("Ещё.\n", encoding="utf-8")
        self.acquire("import", g)
        code, _ = run(self.root, "export", "--project", "demo", env={"STRAT_CRASH_AT": "export_before_manifest"})
        self.assertEqual(code, 137)
        d = run(self.root, "doctor", "--project", "demo")[1]["data"]["exports"]
        self.assertNotEqual(d["state"], "current")

    def test_two_exports_do_not_interleave(self):
        f = pathlib.Path(self.root, "notes.md")
        f.write_text("Один абзац.\n", encoding="utf-8")
        self.import_and_extract(f)
        ready = pathlib.Path(self.root, "ready-export")
        env = {**os.environ, "STRAT_PAUSE_AT": "export_in_lock", "STRAT_PAUSE_SECONDS": "2", "STRAT_READY_FILE": str(ready)}
        a = subprocess.Popen([sys.executable, str(STRAT), "--root", self.root, "export", "--project", "demo"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: (a.poll() is None and a.kill(), a.wait(), a.stdout.close(), a.stderr.close()))
        deadline = time.time() + 20
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        t0 = time.monotonic()
        code, out = run(self.root, "export", "--project", "demo")
        self.assertEqual(code, 0, out)
        self.assertGreater(time.monotonic() - t0, 1.0, "second export waited for the lock")
        a.wait(timeout=20)
        d = run(self.root, "doctor", "--project", "demo")[1]["data"]["exports"]
        self.assertEqual(d["state"], "current")

    def test_export_dump_is_deterministic(self):
        f = pathlib.Path(self.root, "notes.md")
        f.write_text("Один абзац.\n", encoding="utf-8")
        self.import_and_extract(f)
        run(self.root, "export", "--project", "demo")
        exp = pathlib.Path(self.root, "projects/demo/exports")
        first = {p.name: p.read_bytes() for p in (exp / "dump").iterdir()}
        run(self.root, "export", "--project", "demo")
        self.assertEqual(first, {p.name: p.read_bytes() for p in (exp / "dump").iterdir()})


class Schema(Base):
    def test_v2_project_is_refused(self):
        con = sqlite3.connect(pathlib.Path(self.root, "projects/demo/state.sqlite"))
        con.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        con.commit()
        con.close()
        code, out = run(self.root, "status", "--project", "demo")
        self.assertEqual(out["error_code"], "UNSUPPORTED_SCHEMA")


if __name__ == "__main__":
    unittest.main()
