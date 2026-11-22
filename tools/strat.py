#!/usr/bin/env python3
"""strat - the only writer of an agent project's state (plan v1.3, build steps 1-5).

Python stdlib only (PDF needs the optional, pinned `pypdf`). Every command prints one JSON envelope
on stdout, also on failure:
  {"status": "ok"|"skipped"|"error", "error_code": ..., "retryable": bool, "data": {...}}

Commands:
  init     --project P                         create projects/P (state.sqlite, blobs/, exports/)
  doctor   --project P                         environment + store + export-integrity checks
  lease    --project P --session S [--takeover] acquire the single-writer lease -> fencing token
  import   --project P --session S --token T --run R --step N --file F
                                               copy a local file in as an immutable source version
  fetch    --project P --session S --token T --run R --step N --url U
                                               download a web page/file as an immutable source version;
                                               only hosts on the project's allowlist (see `allow`)
  allow    --project P --session S --token T --run R --step N --pattern HOST (--reason TEXT | --revoke)
                                               add/revoke an allowed source host (the human's choice)
  extract  --project P --session S --token T --source-version SV
                                               split a version into addressable segments (idempotent)
  locate   --project P --source-version SV --exact Q [--prefix A] [--suffix B] [--hint H]
                                               resolve a text quote: unique match or explicit unresolved
  export   --project P                         write readable exports (incl. report.md) + content-hash manifest
  status   --project P                         lease, ops, sources
  context  --project P                         resume summary for a new chat: stage, next step, open items, cost
  read     --project P --source-version SV [--around QUOTE] [--chars N]
                                               extracted text of a version (whole but capped, or a window
                                               around a quote) so the agent can see what to quote
  abandon  --project P --session S --token T --op OP_ID --reason TEXT
                                               close an unfinished operation as failed, so the stage moves on
Step 5 (tools/strat_ledger.py, strat_report.py, strat_archive.py, strat_search.py):
  cost     --project P --session S --token T --run R --step N --stage STAGE --kind model|search|tool|human
           --amount X --unit usd|rub|tokens|minutes --basis ide|measured|estimate [--note TEXT]
                                               log what the work cost; an unlogged kind is unknown, not zero
  archive  --project P --out FILE.zip          portable copy: DB snapshot + referenced blobs + hashed manifest
  restore  --file FILE.zip --project NEWID     unpack into a NEW project (a fork: old tokens are fenced)
  search   --project P --query Q [--limit N] [--include-history]
                                               find claims/observations/records by wording (read-only)
Evidence layer (tools/strat_evidence.py), writes take --session/--token/--run/--step:
  classify   --source-version SV --source-kind K   official|statistics|review|search|vendor|...
  observe    --source-version SV --exact Q --kind quote|measurement|count|statement [--value/--unit/...]
  claim      --type fact|hypothesis|forecast|synthetic|vendor_claim --text T --scope JSON
             [--supports O,..] [--contradicts O,..] [--depends C/R,..] [--tags demand,..]
  record     --kind option|capability|strategy|message|... --title T [--fields JSON] --depends ids
  revalidate --old SV1 --new SV2                    carry forward unchanged observations, flag the rest
  resolve    --ids X,Y --outcome reaffirm|supersede|withdraw --reason T [--by NEW]   close a review
  impact     --ids X,Y                              everything resting on X or Y, transitively
  validate                                          gates (must pass) + flags (for human review)

Operation identity is the pair (run, step); files on disk are named by a generated op id, never by
user text. Re-running a (run, step) finishes or skips the earlier attempt instead of repeating it.
A completed import/fetch is immutable: new content goes in under a new step, as a new version.

Test hooks: STRAT_CRASH_AT=<point> dies there; STRAT_PAUSE_AT=<point> writes STRAT_READY_FILE and
sleeps STRAT_PAUSE_SECONDS. STRAT_TEST_ALLOW_LOOPBACK=1 lets fetch reach 127.0.0.1 only (tests;
`doctor` reports it when set). Private, link-local and other loopback ranges stay blocked.
"""
import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import ssl
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.modules.setdefault("strat", sys.modules[__name__])   # one module identity when run as a script
import strat_extract  # noqa: E402

SCHEMA_VERSION = 8
MIGRATABLE = ("7",)              # forward migrations that exist (connect() applies them); older -> refused
RAW_VERSION = "raw-1"            # what acquire stores: bytes only
BUSY_TIMEOUT_MS = int(os.environ.get("STRAT_BUSY_TIMEOUT_MS", "3000"))
LEASE_MINUTES = 30
FETCH_MAX_BYTES = 25 * 1024 * 1024
FETCH_TIMEOUT_S = float(os.environ.get("STRAT_FETCH_TIMEOUT_S", "20"))   # whole download, not per read
FETCH_MAX_REDIRECTS = 5
FETCH_MIN_TEXT_CHARS = int(os.environ.get("STRAT_FETCH_MIN_TEXT", "200"))   # less extracted text = not a source
MACOS_CA_FILE = "/etc/ssl/cert.pem"
READ_DEFAULT_CHARS, READ_MAX_CHARS = 6000, 50000
USER_AGENT = "strat-agent/0.2 (+competition prototype; respects robots via human review)"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
OP_ID = re.compile(r"^op-[0-9a-f]{32}$")
RESPONSE_KEYS = ("final_url", "http_status", "content_type")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lease (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  owner_session TEXT, fencing_token INTEGER NOT NULL DEFAULT 0, expires_at TEXT);
CREATE TABLE IF NOT EXISTS operation (
  op_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_id TEXT NOT NULL,
  tool TEXT NOT NULL, input_hash TEXT NOT NULL, input_uri TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','succeeded','failed','unknown')),
  result_ref TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE (run_id, step_id));
CREATE TABLE IF NOT EXISTS op_attempt (
  op_id TEXT NOT NULL REFERENCES operation(op_id), attempt_no INTEGER NOT NULL,
  claim_token TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK (outcome IN ('pending','ok','failed','unknown','superseded')),
  cost_kind TEXT NOT NULL DEFAULT 'unknown' CHECK (cost_kind IN ('actual','estimate','unknown','none')),
  cost_value REAL, error_code TEXT, started_at TEXT NOT NULL, PRIMARY KEY (op_id, attempt_no));
CREATE TABLE IF NOT EXISTS source (
  id TEXT PRIMARY KEY, uri TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_version (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES source(id),
  sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL, retrieved_at TEXT NOT NULL,
  access_method TEXT NOT NULL, final_url TEXT, http_status INTEGER, content_type TEXT,
  source_kind TEXT NOT NULL DEFAULT 'unknown',
  origin_group TEXT NOT NULL DEFAULT 'unknown', op_id TEXT NOT NULL UNIQUE REFERENCES operation(op_id));
CREATE TABLE IF NOT EXISTS extraction (
  source_version_id TEXT NOT NULL REFERENCES source_version(id), extraction_version TEXT NOT NULL,
  status TEXT NOT NULL, coverage TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (source_version_id, extraction_version));
CREATE TABLE IF NOT EXISTS segment (
  source_version_id TEXT NOT NULL, extraction_version TEXT NOT NULL, idx INTEGER NOT NULL,
  kind TEXT NOT NULL, hint TEXT NOT NULL, text TEXT NOT NULL, context TEXT NOT NULL,
  span_hash TEXT NOT NULL, context_hash TEXT NOT NULL, flag TEXT,
  PRIMARY KEY (source_version_id, extraction_version, idx),
  FOREIGN KEY (source_version_id, extraction_version) REFERENCES extraction(source_version_id, extraction_version));
CREATE TABLE IF NOT EXISTS source_policy (
  pattern TEXT PRIMARY KEY, covers TEXT NOT NULL CHECK (covers IN ('host','subdomains')),
  reason TEXT NOT NULL, added_at TEXT NOT NULL, revoked_at TEXT);
INSERT OR IGNORE INTO lease (id, fencing_token) VALUES (1, 0);
INSERT OR IGNORE INTO meta VALUES ('revision', '0');
"""


class StratError(Exception):
    def __init__(self, code, message, retryable=False, **extra):
        super().__init__(message)
        self.code, self.message, self.retryable, self.extra = code, message, retryable, extra


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def crash_point(name):
    """Test hook: die without cleanup, as a power cut or kill would."""
    if os.environ.get("STRAT_CRASH_AT") == name:
        os._exit(137)


def pause_point(name):
    """Test hook: signal readiness, then hold still (e.g. inside a transaction)."""
    if os.environ.get("STRAT_PAUSE_AT") == name:
        ready = os.environ.get("STRAT_READY_FILE")
        if ready:
            pathlib.Path(ready).write_text(name, encoding="utf-8")
        time.sleep(float(os.environ.get("STRAT_PAUSE_SECONDS", "5")))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_all(fd, data):
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise StratError("INTERNAL", "short write")
        view = view[n:]


def fsync_dir(path):
    if os.name == "posix":   # directory fsync makes the new entry durable; not available on Windows
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_create_only(final, data):
    """Write data to a temp file, fsync, then hard-link it to `final` (fails if final exists)."""
    tmp = final.with_name(final.name + ".tmp-" + uuid.uuid4().hex[:8])
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(final.parent)


def write_replace(path, data):
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class OsLock:
    """Exclusive OS-held lock on a lock file, released by the OS if the process dies.
    The file is never deleted or replaced; on Windows a fixed byte range (offset 0, 1 byte) is
    locked and never read (reading a locked byte raises PermissionError there)."""

    def __init__(self, path, timeout_s=10.0):
        self.path, self.timeout_s, self.fd = path, timeout_s, None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    os.close(self.fd)
                    raise StratError("BUSY", f"{self.path.name} is held by another process", retryable=True)
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


class Project:
    def __init__(self, root, project):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project):
            raise StratError("VALIDATION_FAILED", "project id: 1-64 letters, digits, - and _")
        self.dir = pathlib.Path(root, "projects", project).resolve()
        self.db_path = self.dir / "state.sqlite"
        self.blobs = self.dir / "blobs" / "sha256"
        self.incoming = self.dir / "blobs" / ".incoming"
        self.exports = self.dir / "exports"
        self._cons = []

    def connect(self):
        if not self.db_path.exists():
            raise StratError("NOT_FOUND", f"no project at {self.dir}; run init")
        # isolation_level=None: we issue BEGIN IMMEDIATE ourselves; no shared cache.
        con = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        self._cons.append(con)
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA synchronous = FULL")
        con.row_factory = sqlite3.Row
        # CREATE TABLE IF NOT EXISTS is not a migration: refuse any other schema version explicitly
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() \
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() else None
        found = row["value"] if row else None
        if found in MIGRATABLE:
            found = migrate(con)
        if found != str(SCHEMA_VERSION):
            raise StratError("UNSUPPORTED_SCHEMA", f"project schema {found!r}, this strat needs {SCHEMA_VERSION}; "
                                                   "no migration exists - start a new project or use the matching strat")
        return con

    def close(self):
        for con in self._cons:
            con.close()
        self._cons.clear()

    def blob_path(self, digest):
        return self.blobs / digest[:2] / digest

    def incoming_files(self, op_id, attempt_no):
        stem = f"{op_id}.{attempt_no}"
        return self.incoming / f"{stem}.part", self.incoming / f"{stem}.receipt.json"


def migrate(con):
    """Forward-migrate an older project in place, one version at a time, under one write lock.
    7 -> 8: record.kind gains the final-document kinds (section, calc, forecast_row, ...)."""
    import strat_evidence
    con.execute("PRAGMA foreign_keys = OFF")   # the record table is rebuilt; nothing references it
    try:
        begin_immediate(con)
        found = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        if found == "7":   # re-read inside the lock: another process may have migrated meanwhile
            strat_evidence.migrate_7_to_8(con)
            con.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
            found = "8"
        con.execute("COMMIT")
    finally:
        con.execute("PRAGMA foreign_keys = ON")
    return found


def sqlite_error(e):
    """Map by SQLite result code, not message text; keep the extended name."""
    name = getattr(e, "sqlite_errorname", None)   # Python 3.11+
    if name is None:   # older Python: only the message is available
        name = "SQLITE_BUSY" if "database is locked" in str(e) else "SQLITE_UNKNOWN"
    if name.startswith("SQLITE_BUSY"):
        return StratError("BUSY", "another writer holds the database; retry later", retryable=True,
                          sqlite_error=name)
    if name.startswith("SQLITE_LOCKED"):
        return StratError("LOCKED", "lock conflict inside this connection (shared cache is unsupported)",
                          sqlite_error=name)
    if isinstance(e, sqlite3.IntegrityError):
        return StratError("CONFLICT", f"constraint violated: {e}", sqlite_error=name)
    return StratError("INTERNAL", f"sqlite: {e}", sqlite_error=name)


def begin_immediate(con):
    try:
        con.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as e:
        raise sqlite_error(e)


def write_tx(con, session, token):
    """BEGIN IMMEDIATE + fencing check. Never call network or model code inside."""
    begin_immediate(con)
    row = con.execute("SELECT owner_session, fencing_token FROM lease WHERE id = 1").fetchone()
    if row["owner_session"] != session or row["fencing_token"] != token:
        con.execute("ROLLBACK")
        raise StratError("FENCED", f"session {session} token {token} is not the current lease "
                                   f"(last committed lease: {row['owner_session']} token {row['fencing_token']})")


def bump_revision(con):
    con.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'")


# ---------------------------------------------------------------- network guard (D09)

def _blocked_ip(ip):
    addr = ipaddress.ip_address(ip)
    if os.environ.get("STRAT_TEST_ALLOW_LOOPBACK") == "1" and str(addr) == "127.0.0.1":
        return False
    return (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast
            or addr.is_reserved or addr.is_unspecified)


def norm_host(host):
    """Lower-case, no trailing dot, IDNA (punycode) form, so «пример.рф» and xn--e1afmkfd.xn--p1ai match."""
    host = (host or "").strip().strip("[]").rstrip(".").lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def host_allowed(host, patterns):
    """A pattern allows that host and its subdomains (example.ru allows www.example.ru, not badexample.ru);
    a pattern written "=host" allows that exact host only (R037: a site whose subdomains carry user content).
    IP literals match exactly."""
    h = norm_host(host)
    return any(h == p[1:] if p.startswith("=") else h == p or h.endswith("." + p) for p in patterns)


COVERS = {"host": "только этот сайт", "subdomains": "сайт и все его поддомены"}


def allowed_patterns(con):
    return [("=" if r["covers"] == "host" else "") + r["pattern"]
            for r in con.execute("SELECT pattern, covers FROM source_policy WHERE revoked_at IS NULL")]


def check_url(url, allowed=None):
    """Allow only http(s) to allowlisted hosts with public addresses. Checked for the first URL and
    every redirect; the allowlist is checked before DNS, so a refused host is never even resolved.
    This is NOT connection-level enforcement: DNS can change between this check and the connect
    (rebinding) - a documented residual risk. `allowed=None` skips the allowlist (internal use only)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise StratError("BLOCKED", f"only http/https URLs are fetched, got {url!r}", reason="scheme")
    if u.username or u.password:
        raise StratError("BLOCKED", "URLs with embedded credentials are refused", reason="credentials")
    if allowed is not None and not host_allowed(u.hostname, allowed):
        raise StratError("BLOCKED", f"{u.hostname} is not on this project's source allowlist; ask the human "
                                    "whether to allow it (strat allow)", reason="not_allowed",
                         host=norm_host(u.hostname), allowed=sorted(allowed))
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise StratError("NETWORK", f"cannot resolve {u.hostname}: {e}", retryable=True)
    for info in infos:
        if _blocked_ip(info[4][0]):
            raise StratError("BLOCKED", f"{u.hostname} resolves to a non-public address {info[4][0]}",
                             reason="private_address")


def ssl_context():
    """(context, CA source). Verification always stays on. Stock python.org builds on macOS ship without a
    CA file, so every HTTPS fetch fails CERTIFICATE_VERIFY_FAILED (rehearsal R039 #1): when SSL_CERT_FILE is
    unset, also trust certifi's bundle if installed, else the system bundle at /etc/ssl/cert.pem."""
    ctx = ssl.create_default_context()
    if os.environ.get("SSL_CERT_FILE"):
        return ctx, "SSL_CERT_FILE"
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
        return ctx, "certifi"
    except (ImportError, OSError, ssl.SSLError):
        pass
    if os.path.isfile(MACOS_CA_FILE):
        try:
            ctx.load_verify_locations(cafile=MACOS_CA_FILE)
            return ctx, MACOS_CA_FILE
        except (OSError, ssl.SSLError):
            pass
    return ctx, "system default"


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, deadline, allowed):
        self.count, self.deadline, self.allowed = 0, deadline, allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.count += 1
        if self.count > FETCH_MAX_REDIRECTS:
            raise StratError("BLOCKED", f"more than {FETCH_MAX_REDIRECTS} redirects", reason="redirects")
        left = remaining(self.deadline)
        check_url(newurl, self.allowed)
        # urllib opens the next hop with req.timeout, so give it only what is left of the budget
        req.timeout = left
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise StratError("TIMEOUT", f"download exceeded {FETCH_TIMEOUT_S:g}s in total", retryable=True)
    return left


def download(url, part, allowed):
    """GET outside any lock; bytes to `part`. Returns response metadata for the receipt."""
    deadline = time.monotonic() + FETCH_TIMEOUT_S   # one budget for redirects, headers and body
    check_url(url, allowed)   # note: getaddrinfo itself can't be time-limited from Python; documented
    # explicit policy: never use a system/env proxy - it would bypass the address checks
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirects(deadline, allowed),
                                         urllib.request.HTTPSHandler(context=ssl_context()[0]))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=remaining(deadline)) as resp, open(part, "wb") as f:
            total = 0
            while True:
                sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(remaining(deadline))   # each read gets only what is left
                remaining(deadline)
                chunk = resp.read1(1 << 14) if hasattr(resp, "read1") else resp.read(1 << 14)
                if not chunk:
                    break
                total += len(chunk)
                if total > FETCH_MAX_BYTES:
                    raise StratError("TOO_LARGE", f"more than {FETCH_MAX_BYTES} bytes")
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
            return {"final_url": resp.geturl(), "http_status": resp.status,
                    "content_type": resp.headers.get("Content-Type", "")}
    except urllib.error.HTTPError as e:
        code = {401: "AUTH_REQUIRED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMITED"}.get(e.code)
        if code is None:
            code = "NETWORK" if e.code >= 500 else "HTTP_ERROR"
        raise StratError(code, f"HTTP {e.code} for {url}", retryable=e.code == 429 or e.code >= 500,
                         http_status=e.code, retry_after=e.headers.get("Retry-After"))
    except urllib.error.URLError as e:
        if isinstance(e.reason, StratError):
            raise e.reason
        if isinstance(e.reason, ssl.SSLCertVerificationError):   # retrying won't help: a setup problem
            raise StratError("TLS", f"{url}: certificate not verified ({e.reason.verify_message}). If this "
                                    "Python has no CA bundle (python.org build on macOS), run its "
                                    "'Install Certificates.command' or set SSL_CERT_FILE=/etc/ssl/cert.pem",
                             retryable=False)
        if isinstance(e.reason, socket.timeout):
            raise StratError("TIMEOUT", f"no response from {url} within {FETCH_TIMEOUT_S:g}s", retryable=True)
        raise StratError("NETWORK", f"{url}: {e.reason}", retryable=True)
    except ssl.SSLCertVerificationError as e:
        raise StratError("TLS", f"{url}: certificate not verified ({e.verify_message}); set "
                                "SSL_CERT_FILE=/etc/ssl/cert.pem or install certificates for this Python")
    except (socket.timeout, TimeoutError):
        raise StratError("TIMEOUT", f"download of {url} exceeded {FETCH_TIMEOUT_S:g}s in total", retryable=True)


def check_body(part, response):
    """A fetched body that carries no readable text is not a source version (rehearsal R039 #3: HTTP 202 with
    an empty body was stored as a good source). Raised as BLOCKED, so the attempt and the op end failed."""
    data = part.read_bytes()
    status = response.get("http_status")
    if not data:
        raise StratError("BLOCKED", f"HTTP {status} with an empty body - nothing to store", reason="empty_body",
                         http_status=status)
    name = urllib.parse.urlsplit(response.get("final_url") or "").path
    segs, extraction, _ = strat_extract.extract(name, data, response.get("content_type") or "")
    chars = sum(len(x["text"]) for x in segs)
    # formats this extractor can't read (PDF without pypdf, scans, unknown types) are still stored as before;
    # a readable page (or a JS shell) with almost no text is not a source
    if chars < FETCH_MIN_TEXT_CHARS and extraction in ("full", "partial", "blocked:dynamic"):
        why = extraction if extraction.startswith("blocked") else f"{chars} characters of text"
        raise StratError("BLOCKED", f"HTTP {status}: the page has almost no readable text ({why}); it is not "
                                    "stored as a source. If a browser shows the page, ask the human to save it "
                                    "and use import", reason="empty_body", http_status=status, text_chars=chars)


# ---------------------------------------------------------------- commands

def cmd_init(p, a):
    p.dir.mkdir(parents=True, exist_ok=True)
    for d in (p.blobs, p.incoming, p.exports):
        d.mkdir(parents=True, exist_ok=True)
    import strat_evidence
    import strat_ledger
    con = sqlite3.connect(p.db_path, isolation_level=None)
    try:
        con.executescript(SCHEMA + strat_evidence.SCHEMA + strat_ledger.SCHEMA)
        con.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    finally:
        con.close()
    return "ok", {"project_dir": str(p.dir)}


def cmd_doctor(p, a):
    con = p.connect()
    checks = {
        "sqlite_version": sqlite3.sqlite_version,
        "integrity_check": con.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_violations": len(con.execute("PRAGMA foreign_key_check").fetchall()),
        "schema_version": con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
        "revision": int(con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0]),
        "pdf_support": strat_extract.PDF_LIB_VERSION or "missing (pip install -r requirements-optional.txt)",
        "test_loopback_allowed": os.environ.get("STRAT_TEST_ALLOW_LOOPBACK") == "1" or None,
        "note": "integrity/FK checks find structural damage only, not well-formed edits",
    }
    for tok in ("unicode61", "trigram"):
        try:
            con.execute(f"CREATE VIRTUAL TABLE temp.fts_probe_{tok} USING fts5(x, tokenize='{tok}')")
            checks[f"fts5_{tok}"] = True
        except sqlite3.OperationalError:
            checks[f"fts5_{tok}"] = False
    # Sync folders break SQLite locking; warn on well-known ones (not exhaustive).
    risky = [s for s in ("Dropbox", "OneDrive", "Mobile Documents", "Google Drive", "Yandex.Disk")
             if s in str(p.dir)]
    checks["sync_folder_warning"] = risky or None
    checks["exports"] = check_exports(p, checks["revision"])
    checks["tls_ca_source"] = ssl_context()[1]
    return "ok", checks


def cmd_lease(p, a):
    con = p.connect()
    begin_immediate(con)
    row = con.execute("SELECT owner_session, fencing_token, expires_at FROM lease WHERE id=1").fetchone()
    held = row["owner_session"] and row["owner_session"] != a.session
    expired = row["expires_at"] is None or row["expires_at"] < now()
    if held and not expired and not a.takeover:
        con.execute("ROLLBACK")
        raise StratError("LEASE_HELD", f"lease held by {row['owner_session']} until {row['expires_at']}; "
                                       "pass --takeover if that chat is finished")
    token = row["fencing_token"] + 1 if row["owner_session"] != a.session or a.takeover else row["fencing_token"]
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=LEASE_MINUTES)).isoformat(timespec="seconds")
    con.execute("UPDATE lease SET owner_session=?, fencing_token=?, expires_at=? WHERE id=1",
                (a.session, token, expires))
    con.execute("COMMIT")
    return "ok", {"session": a.session, "fencing_token": token, "expires_at": expires,
                  "note": "a lease identifies a session, not a single process"}


def cmd_import(p, a):
    src = pathlib.Path(a.file).expanduser().resolve()
    if not src.is_file():
        raise StratError("NOT_FOUND", f"file not found: {a.file}")

    def produce(part):
        shutil.copyfile(src, part)
        with open(part, "rb+") as f:
            os.fsync(f.fileno())
        return {}
    return acquire(p, a, "import", src.as_uri(), produce, local_path=src)


def cmd_fetch(p, a):
    con = p.connect()
    allowed = allowed_patterns(con)   # read once; a fetch in flight keeps the list it started with
    check_url(a.url, allowed)   # refuse early, before any operation is recorded
    def produce(part):
        response = download(a.url, part, allowed)
        check_body(part, response)
        return response
    return acquire(p, a, "fetch", a.url, produce)


def cmd_allow(p, a):
    """The allowlist is the human's choice (brief: «разрешённые источники»); --reason records their words and
    --covers says whether the grant is the exact host or the host with every subdomain, so the human sees the
    reach of a "yes" (R037). It limits where fetch goes; it does not make an allowed page trustworthy - text on
    it is still data, never instructions. Revoking stops future fetches only - fetched versions stay."""
    import strat_evidence as E
    pattern = norm_host(a.pattern)
    if not re.fullmatch(r"[a-z0-9.:-]{1,253}", pattern) or pattern.startswith(".") or "." not in pattern and ":" not in pattern:
        raise StratError("VALIDATION_FAILED", f"pattern must be a host name like example.ru or an IP, got {a.pattern!r}")
    if not a.revoke and not (a.reason or "").strip():
        raise StratError("VALIDATION_FAILED", "--reason is required: record why the human allowed this source")
    if not a.revoke and a.covers not in COVERS:
        raise StratError("VALIDATION_FAILED", "--covers host|subdomains is required: ask the human whether the "
                                              "grant is this exact site or the site with all its subdomains")
    con = p.connect()
    write_tx(con, a.session, a.token)
    prev, input_hash = E.idempotent(con, a, "allow", {"pattern": pattern, "revoke": bool(a.revoke),
                                                       "reason": a.reason or "", "covers": a.covers or ""})
    if prev:
        return "skipped", {"pattern": pattern, "reason": "already recorded"}
    if a.revoke:
        n = con.execute("UPDATE source_policy SET revoked_at=? WHERE pattern=? AND revoked_at IS NULL",
                        (now(), pattern)).rowcount
        if not n:
            con.execute("ROLLBACK")
            raise StratError("NOT_FOUND", f"{pattern} is not on the allowlist")
    else:
        con.execute("INSERT INTO source_policy VALUES (?,?,?,?,NULL) ON CONFLICT(pattern) DO UPDATE SET "
                    "covers=excluded.covers, reason=excluded.reason, added_at=excluded.added_at, revoked_at=NULL",
                    (pattern, a.covers, a.reason, now()))
    E.finish_op(con, a, "allow", input_hash, pattern)
    bump_revision(con)
    con.execute("COMMIT")
    return "ok", {"pattern": pattern, "revoked": bool(a.revoke),
                  "covers": None if a.revoke else COVERS[a.covers], "allowed": allowed_patterns(con)}


def acquire(p, a, tool, uri, produce, local_path=None):
    """Shared pipeline for import and fetch (plan v1.2/v1.3 §7, §11.2):
    tx1 claim attempt -> effect (no lock) -> .part -> receipt -> canonical blob -> tx2 finalize."""
    # input identity = tool + URI; the bytes are recorded per version, not part of the identity
    input_hash = sha256_bytes(json.dumps({"tool": tool, "uri": uri}, sort_keys=True).encode())
    con = p.connect()

    write_tx(con, a.session, a.token)
    op = con.execute("SELECT * FROM operation WHERE run_id=? AND step_id=?", (a.run, a.step)).fetchone()
    if op and op["input_hash"] != input_hash:
        con.execute("ROLLBACK")
        raise StratError("CONFLICT", f"run {a.run!r} step {a.step!r} was already used with different input")
    if op and op["status"] == "succeeded":
        con.execute("ROLLBACK")
        return already_done(p, con, op, local_path)
    if op is None:
        op_id = "op-" + uuid.uuid4().hex
        con.execute("INSERT INTO operation VALUES (?,?,?,?,?,?,'pending',NULL,?,?)",
                    (op_id, a.run, a.step, tool, input_hash, uri, now(), now()))
        op = con.execute("SELECT * FROM operation WHERE op_id=?", (op_id,)).fetchone()
    op_id = op["op_id"]
    last = con.execute("SELECT * FROM op_attempt WHERE op_id=? ORDER BY attempt_no DESC LIMIT 1",
                       (op_id,)).fetchone()
    receipt = find_receipt(p, op, last["attempt_no"]) if last else None
    claim = uuid.uuid4().hex
    if receipt:
        attempt_no = last["attempt_no"]        # its bytes arrived; finish it under a fresh claim, no re-read
        con.execute("UPDATE op_attempt SET claim_token=? WHERE op_id=? AND attempt_no=?", (claim, op_id, attempt_no))
    else:
        if last and last["outcome"] == "pending":
            con.execute("UPDATE op_attempt SET outcome='unknown' WHERE op_id=? AND attempt_no=?",
                        (op_id, last["attempt_no"]))
        attempt_no = (last["attempt_no"] if last else 0) + 1
        # a failed or abandoned op is retried explicitly by rerunning its (run, step): it is open again
        con.execute("UPDATE operation SET status='pending', updated_at=? WHERE op_id=? AND status='failed'",
                    (now(), op_id))
        con.execute("INSERT INTO op_attempt (op_id, attempt_no, claim_token, outcome, cost_kind, started_at) "
                    "VALUES (?,?,?,'pending','none',?)", (op_id, attempt_no, claim, now()))
    con.execute("COMMIT")
    crash_point("after_pending")
    pause_point("after_pending")

    part, rpath = p.incoming_files(op_id, attempt_no)
    if receipt is None:
        try:
            response = produce(part)
        except StratError as e:   # record the failed attempt
            part.unlink(missing_ok=True)
            write_tx(con, a.session, a.token)
            con.execute("UPDATE op_attempt SET outcome='failed', error_code=? WHERE op_id=? AND attempt_no=? "
                        "AND claim_token=?", (e.code, op_id, attempt_no, claim))
            if not e.retryable:   # final: close the op so it never holds the stage (rehearsal R039 #6);
                con.execute("UPDATE operation SET status='failed', updated_at=? WHERE op_id=? AND status='pending'",
                            (now(), op_id))   # rerunning the same (run, step) still retries it
            con.execute("COMMIT")
            raise
        crash_point("after_part")
        receipt = {"op_id": op_id, "attempt_no": attempt_no, "input_hash": input_hash, "tool": tool,
                   "uri": uri, "sha256": sha256_file(part), "byte_length": part.stat().st_size,
                   "extraction_version": RAW_VERSION, "part": part.name}
        receipt.update({k: response[k] for k in RESPONSE_KEYS if k in response})
        try:
            publish_create_only(rpath, json.dumps(receipt, sort_keys=True).encode())
        except FileExistsError:
            raise StratError("CONFLICT", f"receipt for {op_id} attempt {attempt_no} already exists")
        crash_point("after_receipt")

    # publish the canonical blob BEFORE the DB commit (plan v1.2)
    publish_blob(p, receipt)
    crash_point("after_publish")

    write_tx(con, a.session, a.token)
    pause_point("in_tx2")
    op = con.execute("SELECT * FROM operation WHERE op_id=?", (op_id,)).fetchone()
    mine = con.execute("SELECT claim_token FROM op_attempt WHERE op_id=? AND attempt_no=?",
                       (op_id, attempt_no)).fetchone()
    if op["status"] == "succeeded":
        con.execute("ROLLBACK")
        return already_done(p, con, op, local_path, note="finished by another process; this attempt discarded")
    if mine is None or mine["claim_token"] != claim:
        con.execute("ROLLBACK")
        raise StratError("CONFLICT", "another process took over this operation; rerun to finish it",
                         retryable=True)
    if con.execute("SELECT 1 FROM op_attempt WHERE op_id=? AND attempt_no>?", (op_id, attempt_no)).fetchone():
        con.execute("ROLLBACK")
        raise StratError("CONFLICT", "a newer attempt exists; rerun to finish it", retryable=True)
    src_row = con.execute("SELECT id FROM source WHERE uri=?", (uri,)).fetchone()
    source_id = src_row["id"] if src_row else "S-" + uuid.uuid4().hex[:12]
    if not src_row:
        con.execute("INSERT INTO source VALUES (?,?,?)", (source_id, uri, now()))
    sv_id = "SV-" + uuid.uuid4().hex[:12]
    con.execute("INSERT INTO source_version (id, source_id, sha256, byte_length, retrieved_at, access_method, "
                "final_url, http_status, content_type, op_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sv_id, source_id, receipt["sha256"], receipt["byte_length"], now(),
                 "local_import" if tool == "import" else "http_get", receipt.get("final_url"),
                 receipt.get("http_status"), receipt.get("content_type"), op_id))
    con.execute("UPDATE operation SET status='succeeded', result_ref=?, updated_at=? WHERE op_id=?",
                (sv_id, now(), op_id))
    resumed = bool(last and attempt_no == last["attempt_no"])
    # a resumed attempt's cost is unknown, never zero; a plain local copy or GET costs nothing
    con.execute("UPDATE op_attempt SET outcome='ok', cost_kind=? WHERE op_id=? AND attempt_no=?",
                ("unknown" if resumed else "none", op_id, attempt_no))
    con.execute("UPDATE op_attempt SET outcome='superseded' WHERE op_id=? AND attempt_no<? AND outcome='pending'",
                (op_id, attempt_no))
    bump_revision(con)
    con.execute("COMMIT")
    crash_point("after_commit")
    cleanup_incoming(p, con, op_id)
    return "ok", {"op_id": op_id, "run": a.run, "step": a.step, "source_version_id": sv_id,
                  "sha256": receipt["sha256"], "attempt_no": attempt_no, "resumed": resumed,
                  **{k: receipt[k] for k in RESPONSE_KEYS if k in receipt}}


def already_done(p, con, op, local_path, note="already succeeded"):
    sv = con.execute("SELECT sha256 FROM source_version WHERE id=?", (op["result_ref"],)).fetchone()
    data = {"op_id": op["op_id"], "source_version_id": op["result_ref"], "reason": note}
    if sv and local_path and local_path.is_file() and sha256_file(local_path) != sv["sha256"]:
        data["warning"] = "file changed since this import; the recorded version is kept - import under a new step"
    cleanup_incoming(p, con, op["op_id"])
    return "skipped", data


def cmd_extract(p, a):
    con = p.connect()
    sv = con.execute("SELECT sv.*, s.uri FROM source_version sv JOIN source s ON s.id = sv.source_id "
                     "WHERE sv.id=?", (a.source_version,)).fetchone()
    if sv is None:
        raise StratError("NOT_FOUND", f"no source version {a.source_version}")
    ev = strat_extract.EXTRACTION_VERSION
    done = con.execute("SELECT status, coverage FROM extraction WHERE source_version_id=? AND extraction_version=?",
                       (sv["id"], ev)).fetchone()
    if done:
        return "skipped", {"source_version_id": sv["id"], "extraction_version": ev, "status": done["status"],
                           "coverage": json.loads(done["coverage"]), "reason": "already extracted"}
    blob = p.blob_path(sv["sha256"])
    data = blob.read_bytes()
    if sha256_bytes(data) != sv["sha256"]:
        raise StratError("INTERNAL", f"blob for {sv['id']} fails its hash check")
    name = urllib.parse.urlsplit(sv["final_url"] or sv["uri"]).path
    segs, status, coverage = strat_extract.extract(name, data, sv["content_type"] or "")   # no lock held
    write_tx(con, a.session, a.token)
    if con.execute("SELECT 1 FROM extraction WHERE source_version_id=? AND extraction_version=?",
                   (sv["id"], ev)).fetchone():
        con.execute("ROLLBACK")
        return "skipped", {"source_version_id": sv["id"], "extraction_version": ev, "reason": "extracted meanwhile"}
    con.execute("INSERT INTO extraction VALUES (?,?,?,?,?)",
                (sv["id"], ev, status, json.dumps(coverage, ensure_ascii=False, sort_keys=True), now()))
    con.executemany("INSERT INTO segment VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(sv["id"], ev, s["idx"], s["kind"], s["hint"], s["text"], s["context"],
                      sha256_bytes(s["text"].encode()), sha256_bytes(s["context"].encode()), s.get("flag"))
                     for s in segs])
    bump_revision(con)
    con.execute("COMMIT")
    return "ok", {"source_version_id": sv["id"], "extraction_version": ev, "status": status,
                  "segments": len(segs), "coverage": coverage}


def cmd_locate(p, a):
    con = p.connect()
    ev = a.extraction_version or strat_extract.EXTRACTION_VERSION
    rows = [dict(r) for r in con.execute(
        "SELECT idx, kind, hint, text, context, span_hash, context_hash FROM segment "
        "WHERE source_version_id=? AND extraction_version=? ORDER BY idx", (a.source_version, ev))]
    if not rows:
        raise StratError("NOT_FOUND", f"no segments for {a.source_version} / {ev}; run extract")
    r = strat_extract.locate(rows, a.exact, a.prefix or "", a.suffix or "", a.hint)
    if not r["resolved"]:
        return "ok", {"resolved": False, "reason": r["reason"], "candidates": r["candidates"],
                      "source_version_id": a.source_version, "extraction_version": ev}
    s = r["segment"]
    return "ok", {"resolved": True, "source_version_id": a.source_version, "extraction_version": ev,
                  "segment_idx": s["idx"], "hint": s["hint"], "offset": r["offset"],
                  "context": s["context"], "span_hash": s["span_hash"], "context_hash": s["context_hash"]}


EXPORT_TABLES = ("source", "source_version", "extraction", "segment", "observation", "claim", "record", "edge",
                 "review_event", "operation", "op_attempt", "source_policy", "cost_entry")


def cmd_export(p, a):
    """VIEW export: readable Markdown + deterministic JSONL dump + manifest with content hashes (C07).
    Not the portable archive (that is `archive`: DB snapshot + blobs). report.md is the one readable document.
    One export at a time (OS lock); data read in one transaction (one DB revision); the manifest is
    written last, so a crash mid-export leaves files that `doctor` reports as edited, never current."""
    p.exports.mkdir(parents=True, exist_ok=True)
    with OsLock(p.exports / ".export.lock"):
        pause_point("export_in_lock")
        return _export_locked(p)


def _export_locked(p):
    con = p.connect()
    con.execute("BEGIN")
    revision = int(con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
    tables = {t: [dict(r) for r in con.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3")] for t in EXPORT_TABLES}
    seg_counts = {r["source_version_id"]: r["n"] for r in con.execute(
        "SELECT source_version_id, COUNT(*) n FROM segment GROUP BY 1")}
    import strat_evidence
    claims_md = strat_evidence.claims_markdown(con)
    import strat_context
    context_md = strat_context.markdown(strat_context.build(p, con, revision))
    import strat_report
    report_md = strat_report.build(con, revision)
    document_md = strat_report.document(con, revision)
    con.execute("COMMIT")
    files = {}
    for t, rows in tables.items():
        body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()
        files[f"dump/{t}.jsonl"] = body
    uri = {s["id"]: s["uri"] for s in tables["source"]}
    ext = {e["source_version_id"]: e for e in tables["extraction"]}
    lines = ["# Источники", "", f"Ревизия базы: {revision}. Файл создан автоматически - не редактировать: "
             "изменения вносятся через strat.", "",
             "| Версия | Источник | Получено | Байт | Извлечение | Сегментов |", "|---|---|---|---|---|---|"]
    for v in tables["source_version"]:
        e = ext.get(v["id"])
        lines.append(f"| {v['id']} | {uri.get(v['source_id'], '')} | {v['retrieved_at']} | {v['byte_length']} | "
                     f"{e['status'] if e else 'не выполнено'} | {seg_counts.get(v['id'], 0)} |")
    files["sources.md"] = ("\n".join(lines) + "\n").encode()
    files["claims.md"] = claims_md
    files["context.md"] = context_md
    files["report.md"] = report_md
    files["document.md"] = document_md
    for rel, body in files.items():
        path = p.exports / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        write_replace(path, body)
    crash_point("export_before_manifest")
    manifest = {"kind": "view_export", "exported_revision": revision, "exported_at": now(),
                "files": {rel: sha256_bytes(body) for rel, body in sorted(files.items())}}
    write_replace(p.exports / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True).encode())
    return "ok", {"kind": "view_export", "exported_revision": revision, "files": sorted(files),
                  "note": "readable views only; the portable copy (DB snapshot + blobs) is `archive`"}


def check_exports(p, revision):
    mpath = p.exports / "manifest.json"
    if not mpath.exists():
        return {"state": "none"}
    try:
        m = json.loads(mpath.read_text(encoding="utf-8"))
    except ValueError:
        return {"state": "manifest_unreadable"}
    edited = [rel for rel, h in m.get("files", {}).items()
              if not (p.exports / rel).is_file() or sha256_file(p.exports / rel) != h]
    return {"state": "edited" if edited else ("stale" if m.get("exported_revision") != revision else "current"),
            "edited_files": edited, "exported_revision": m.get("exported_revision"), "db_revision": revision}


def cmd_status(p, a):
    con = p.connect()
    lease = dict(con.execute("SELECT owner_session, fencing_token, expires_at FROM lease").fetchone())
    ops = [dict(r) for r in con.execute("SELECT op_id, run_id, step_id, tool, status, result_ref FROM operation ORDER BY created_at")]
    attempts = [dict(r) for r in con.execute("SELECT op_id, attempt_no, outcome, cost_kind, error_code FROM op_attempt")]
    svs = [dict(r) for r in con.execute("SELECT id, source_id, sha256, op_id FROM source_version")]
    missing = [s["id"] for s in svs if not p.blob_path(s["sha256"]).exists()]
    return "ok", {"lease": {**lease, "note": "last committed lease, not proof of the current lock holder"},
                  "operations": ops, "attempts": attempts, "source_versions": svs,
                  "source_versions_missing_blob": missing}


def cmd_abandon(p, a):
    """Close an unfinished operation (pending/unknown, or failed) as failed with the reason, so `context`
    stops waiting on it (rehearsal R039 #6: a fetch that kept failing held `resume_operation` forever).
    History stays: the attempts are kept, the last open one is marked failed (ABANDONED)."""
    if not (a.reason or "").strip():
        raise StratError("VALIDATION_FAILED", "--reason is required: say why the operation is given up")
    con = p.connect()
    write_tx(con, a.session, a.token)
    op = con.execute("SELECT * FROM operation WHERE op_id=?", (a.op,)).fetchone()
    if op is None:
        con.execute("ROLLBACK")
        raise StratError("NOT_FOUND", f"no operation {a.op}")
    if op["status"] == "succeeded":
        con.execute("ROLLBACK")
        raise StratError("CONFLICT", f"{a.op} already succeeded ({op['result_ref']}); nothing to abandon")
    note = f"abandoned: {a.reason.strip()}"
    if op["status"] == "failed" and op["result_ref"] == note:
        con.execute("ROLLBACK")
        return "skipped", {"op_id": a.op, "status": "failed", "reason": "already abandoned"}
    con.execute("UPDATE op_attempt SET outcome='failed', error_code='ABANDONED' WHERE op_id=? "
                "AND outcome IN ('pending','unknown')", (a.op,))
    con.execute("UPDATE operation SET status='failed', result_ref=?, updated_at=? WHERE op_id=?",
                (note, now(), a.op))
    bump_revision(con)
    con.execute("COMMIT")
    cleanup_incoming(p, con, a.op)
    return "ok", {"op_id": a.op, "run": op["run_id"], "step": op["step_id"], "tool": op["tool"],
                  "status": "failed", "reason": note,
                  "note": "rerunning the same run/step retries it; a different input needs a new step"}


def cmd_read(p, a):
    """Read-only: the extracted text of a source version, as `[#idx hint] text` lines (idx/hint are what
    observe/locate take as --hint). Whole text capped at --chars, or a window of --chars around --around."""
    con = p.connect()
    if con.execute("SELECT 1 FROM source_version WHERE id=?", (a.source_version,)).fetchone() is None:
        raise StratError("NOT_FOUND", f"no source version {a.source_version}")
    ev = strat_extract.EXTRACTION_VERSION
    ex = con.execute("SELECT status FROM extraction WHERE source_version_id=? AND extraction_version=?",
                     (a.source_version, ev)).fetchone()
    if ex is None:
        raise StratError("NOT_FOUND", f"{a.source_version} is not extracted yet; run extract")
    try:
        chars = int(a.chars) if a.chars is not None else READ_DEFAULT_CHARS
    except ValueError:
        chars = 0
    if not 1 <= chars <= READ_MAX_CHARS:
        raise StratError("VALIDATION_FAILED", f"--chars must be 1..{READ_MAX_CHARS}")
    text = "\n".join(f"[#{r['idx']} {r['hint']}] {r['text']}" for r in con.execute(
        "SELECT idx, hint, text FROM segment WHERE source_version_id=? AND extraction_version=? ORDER BY idx",
        (a.source_version, ev)))
    out = {"source_version_id": a.source_version, "extraction_status": ex["status"], "total_chars": len(text)}
    if a.around:
        pos, n = text.find(a.around), text.count(a.around)
        if pos < 0 and len(text.lower()) == len(text):   # case-insensitive fallback, same offsets
            pos, n = text.lower().find(a.around.lower()), text.lower().count(a.around.lower())
        if pos < 0:
            return "ok", {**out, "found": False, "matches": 0, "text": "",
                          "note": "quote not in the extracted text; read without --around to see it"}
        start = max(0, pos + len(a.around) // 2 - chars // 2)
        end = min(len(text), start + chars)
        start = max(0, end - chars)
        return "ok", {**out, "found": True, "matches": n, "start": start, "end": end,
                      "truncated": start > 0 or end < len(text), "text": text[start:end]}
    return "ok", {**out, "start": 0, "end": min(chars, len(text)), "truncated": len(text) > chars,
                  "text": text[:chars]}


# ---------------------------------------------------------------- helpers

def find_receipt(p, op, attempt_no):
    """Attach a receipt only if its whole identity matches the durable operation and the bytes verify."""
    part, rpath = p.incoming_files(op["op_id"], attempt_no)
    if not rpath.exists():
        return None
    try:
        r = json.loads(rpath.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    expected = {"op_id": op["op_id"], "attempt_no": attempt_no, "input_hash": op["input_hash"],
                "tool": op["tool"], "uri": op["input_uri"], "extraction_version": RAW_VERSION,
                "part": part.name}
    allowed = set(expected) | {"sha256", "byte_length"} | (set(RESPONSE_KEYS) if op["tool"] == "fetch" else set())
    if not isinstance(r, dict) or not (set(expected) | {"sha256", "byte_length"}) <= set(r) or not set(r) <= allowed:
        return None
    if any(r[k] != v for k, v in expected.items()):
        return None
    if not isinstance(r["sha256"], str) or not HEX64.match(r["sha256"]) or not isinstance(r["byte_length"], int):
        return None
    for candidate in (p.blob_path(r["sha256"]), part):
        if candidate.is_file() and candidate.stat().st_size == r["byte_length"] \
                and sha256_file(candidate) == r["sha256"]:
            return r
    return None


def publish_blob(p, receipt):
    dest = p.blob_path(receipt["sha256"])
    if dest.exists():
        if sha256_file(dest) != receipt["sha256"]:
            raise StratError("INTERNAL", f"blob {dest.name} exists with wrong content")
        return
    part = p.incoming / pathlib.Path(receipt["part"]).name   # never follow path components from a receipt
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp-" + uuid.uuid4().hex[:8])
    shutil.copyfile(part, tmp)
    with open(tmp, "rb+") as f:
        os.fsync(f.fileno())
    if sha256_file(tmp) != receipt["sha256"]:
        tmp.unlink()
        raise StratError("INTERNAL", "bytes changed between receipt and publish")
    os.replace(tmp, dest)
    fsync_dir(dest.parent)


def cleanup_incoming(p, con, op_id):
    """Delete exactly the files this op owns - names are generated, never derived from user text."""
    if not OP_ID.match(op_id):
        return
    for (n,) in con.execute("SELECT attempt_no FROM op_attempt WHERE op_id=?", (op_id,)):
        for f in p.incoming_files(op_id, n):
            f.unlink(missing_ok=True)


# ---------------------------------------------------------------- entry

def main(argv=None):
    ap = argparse.ArgumentParser(prog="strat", description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.environ.get("STRAT_ROOT", "."))
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "doctor", "status", "export"):
        sub.add_parser(name).add_argument("--project", required=True)
    lp = sub.add_parser("lease")
    lp.add_argument("--project", required=True)
    lp.add_argument("--session", required=True)
    lp.add_argument("--takeover", action="store_true")
    al = sub.add_parser("allow")
    for f in ("--project", "--session", "--run", "--step", "--pattern"):
        al.add_argument(f, required=True)
    al.add_argument("--token", type=int, required=True)
    al.add_argument("--reason")
    al.add_argument("--covers", choices=sorted(COVERS))
    al.add_argument("--revoke", action="store_true")
    for name, target in (("import", "--file"), ("fetch", "--url")):
        sp = sub.add_parser(name)
        for f in ("--project", "--session", "--run", "--step", target):
            sp.add_argument(f, required=True)
        sp.add_argument("--token", type=int, required=True)
    ep = sub.add_parser("extract")
    for f in ("--project", "--session", "--source-version"):
        ep.add_argument(f, required=True)
    ep.add_argument("--token", type=int, required=True)
    lo = sub.add_parser("locate")
    for f in ("--project", "--source-version", "--exact"):
        lo.add_argument(f, required=True)
    for f in ("--prefix", "--suffix", "--hint", "--extraction-version"):
        lo.add_argument(f)
    rd = sub.add_parser("read")
    for f in ("--project", "--source-version"):
        rd.add_argument(f, required=True)
    rd.add_argument("--around")
    rd.add_argument("--chars")
    ab = sub.add_parser("abandon")
    for f in ("--project", "--session", "--op", "--reason"):
        ab.add_argument(f, required=True)
    ab.add_argument("--token", type=int, required=True)
    import strat_evidence
    import strat_context
    import strat_ledger
    import strat_archive
    import strat_search
    for module in (strat_evidence, strat_ledger, strat_archive, strat_search):
        module.register(sub)
    sub.add_parser("context").add_argument("--project", required=True)
    a = ap.parse_args(argv)
    p = None
    try:
        p = Project(a.root, a.project)
        status, data = {"init": cmd_init, "doctor": cmd_doctor, "lease": cmd_lease, "import": cmd_import,
                        "fetch": cmd_fetch, "allow": cmd_allow, "extract": cmd_extract, "locate": cmd_locate,
                        "export": cmd_export, "status": cmd_status,
                        "read": cmd_read, "abandon": cmd_abandon, "context": strat_context.cmd_context,
                        **strat_evidence.COMMANDS, **strat_ledger.COMMANDS, **strat_archive.COMMANDS,
                        **strat_search.COMMANDS}[a.cmd](p, a)
        out, code = {"status": status, "error_code": None, "retryable": False, "data": data}, 0
    except StratError as e:
        out, code = {"status": "error", "error_code": e.code, "retryable": e.retryable,
                     "data": {"message": e.message, **e.extra}}, 1
    except sqlite3.Error as e:
        err = sqlite_error(e)
        out, code = {"status": "error", "error_code": err.code, "retryable": err.retryable,
                     "data": {"message": err.message, **err.extra}}, 1
    except OSError as e:
        out, code = {"status": "error", "error_code": "IO_ERROR", "retryable": False,
                     "data": {"message": f"{type(e).__name__}: {e}"}}, 1
    except Exception as e:   # the envelope is a contract: never let a traceback be the only output
        out, code = {"status": "error", "error_code": "INTERNAL", "retryable": False,
                     "data": {"message": f"{type(e).__name__}: {e}"}}, 1
    finally:
        if p is not None:
            p.close()
    # the envelope is always UTF-8 bytes, whatever the console or locale encoding is (Windows cp1251/cp1252)
    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
