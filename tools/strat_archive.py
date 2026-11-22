"""Portable archive and restore for strat (build step 5).

archive: a zip with a consistent DB snapshot (sqlite3 backup API), every blob a source version refers to,
and manifest.json (sha256 + size of every member, schema version). Read-only for the project; holds the
export OS lock. In the snapshot copy only, the lease owner is cleared (the fencing token is kept).
restore: into a NEW project only. Every member must be named in the manifest with a strict name, hashes and
schema are verified, total size is capped; work happens in a staging folder that is renamed into place
at the end, so a failure leaves no half-made project. The restored lease has no owner and its fencing
token is incremented: a copy is a fork, and any old chat's token is fenced.
Exports (views) are not archived - run `export` after restore. No blob is ever deleted here.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile

import strat as S

MANIFEST = "manifest.json"
DB_MEMBER = "state.sqlite"
BLOB_MEMBER = re.compile(r"^blobs/sha256/([0-9a-f]{2})/([0-9a-f]{64})$")
MAX_TOTAL = 2 * 1024 ** 3   # 2 GB uncompressed
MAX_MANIFEST = 16 * 1024 * 1024
CHUNK = 1 << 20


def _copy_hashed(src, dst, limit):
    """Copy a stream, return (sha256, size); stop past `limit` bytes."""
    h, n = hashlib.sha256(), 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            return h.hexdigest(), n
        n += len(chunk)
        if n > limit:
            raise S.StratError("TOO_LARGE", "archive member is larger than its manifest says")
        h.update(chunk)
        dst.write(chunk)


def cmd_archive(p, a):
    out = pathlib.Path(a.out).expanduser().resolve()
    if out.exists():
        raise S.StratError("CONFLICT", f"{out} already exists; choose a new file name")
    if not out.parent.is_dir():
        raise S.StratError("NOT_FOUND", f"folder {out.parent} does not exist")
    con = p.connect()   # also refuses other schema versions
    p.exports.mkdir(parents=True, exist_ok=True)
    with S.OsLock(p.exports / ".export.lock"), tempfile.TemporaryDirectory() as td:
        snap_path = pathlib.Path(td, DB_MEMBER)
        snap = sqlite3.connect(snap_path, isolation_level=None)
        try:
            con.backup(snap)   # one consistent copy, whatever writers do meanwhile
            snap.execute("UPDATE lease SET owner_session=NULL, expires_at=NULL WHERE id=1")
            revision = int(snap.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
            digests = sorted({r[0] for r in snap.execute("SELECT sha256 FROM source_version")})
        finally:
            snap.close()
        S.pause_point("archive_after_snapshot")
        bad = [d for d in digests if not p.blob_path(d).is_file() or S.sha256_file(p.blob_path(d)) != d]
        if bad:
            raise S.StratError("BLOB_MISSING", f"{len(bad)} blob(s) referenced by source versions are missing or "
                                               "damaged; the archive would not be complete", blobs=bad)
        files = {DB_MEMBER: snap_path, **{f"blobs/sha256/{d[:2]}/{d}": p.blob_path(d) for d in digests}}
        members = {name: {"sha256": S.sha256_file(path), "size": path.stat().st_size}
                   for name, path in files.items()}
        manifest = {"kind": "strat_archive", "schema_version": S.SCHEMA_VERSION, "project": p.dir.name,
                    "db_revision": revision, "created_at": S.now(), "members": members}
        tmp = out.with_name(out.name + ".tmp-" + uuid.uuid4().hex[:8])
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for name, path in files.items():
                    zf.write(path, name)
                zf.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))
            with zipfile.ZipFile(tmp) as zf:   # verify what was written, member by member
                if sorted(zf.namelist()) != sorted([MANIFEST, *members]):
                    raise S.StratError("INTERNAL", "archive member list differs from its manifest")
                for name, m in members.items():
                    with zf.open(name) as f:
                        h = hashlib.sha256()
                        for chunk in iter(lambda: f.read(CHUNK), b""):
                            h.update(chunk)
                    if h.hexdigest() != m["sha256"]:
                        raise S.StratError("INTERNAL", f"{name} fails its hash check after writing")
            with open(tmp, "rb+") as f:
                os.fsync(f.fileno())
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
    return "ok", {"file": str(out), "sha256": S.sha256_file(out), "schema_version": S.SCHEMA_VERSION,
                  "db_revision": revision, "blobs": len(digests),
                  "note": "lease owner cleared in the copy only; exports are views and are not archived"}


def _check_members(zf):
    """Return the manifest after checking every name against it; nothing outside it, no odd names."""
    names = zf.namelist()
    if len(names) != len(set(names)):
        raise S.StratError("VALIDATION_FAILED", "archive has duplicate member names")
    for n in names:
        if n != MANIFEST and n != DB_MEMBER and not BLOB_MEMBER.match(n):
            raise S.StratError("VALIDATION_FAILED", f"unexpected archive member {n!r}", member=n)
    info = zf.getinfo(MANIFEST) if MANIFEST in names else None
    if info is None or info.file_size > MAX_MANIFEST:
        raise S.StratError("VALIDATION_FAILED", "archive has no readable manifest.json")
    try:
        m = json.loads(zf.read(MANIFEST).decode("utf-8"))
    except ValueError:
        raise S.StratError("VALIDATION_FAILED", "manifest.json is not valid JSON")
    if not isinstance(m, dict) or m.get("kind") != "strat_archive" or not isinstance(m.get("members"), dict):
        raise S.StratError("VALIDATION_FAILED", "manifest.json is not a strat archive manifest")
    if m.get("schema_version") != S.SCHEMA_VERSION and str(m.get("schema_version")) not in S.MIGRATABLE:
        raise S.StratError("UNSUPPORTED_SCHEMA", f"archive schema {m.get('schema_version')!r}, this strat needs "
                                                 f"{S.SCHEMA_VERSION}; no migration exists")
    members = m["members"]
    if set(members) != set(names) - {MANIFEST} or DB_MEMBER not in members:
        raise S.StratError("VALIDATION_FAILED", "archive members and manifest do not match")
    total = 0
    for n, meta in members.items():
        if not isinstance(meta, dict) or not isinstance(meta.get("size"), int) or meta["size"] < 0 \
                or not isinstance(meta.get("sha256"), str) or not S.HEX64.match(meta["sha256"]):
            raise S.StratError("VALIDATION_FAILED", f"manifest entry for {n} is malformed")
        b = BLOB_MEMBER.match(n)
        if b and (b.group(2) != meta["sha256"] or b.group(1) != meta["sha256"][:2]):
            raise S.StratError("VALIDATION_FAILED", f"blob {n} is not named by its hash")
        if zf.getinfo(n).file_size != meta["size"]:
            raise S.StratError("VALIDATION_FAILED", f"{n}: size differs from the manifest")
        total += meta["size"]
    if total > MAX_TOTAL:
        raise S.StratError("TOO_LARGE", f"archive unpacks to {total} bytes; the limit is {MAX_TOTAL}")
    return m


def cmd_restore(p, a):
    src = pathlib.Path(a.file).expanduser().resolve()
    if not src.is_file():
        raise S.StratError("NOT_FOUND", f"file not found: {a.file}")
    if p.dir.exists():
        raise S.StratError("CONFLICT", f"project {p.dir.name} already exists; restore into a new project id")
    p.dir.parent.mkdir(parents=True, exist_ok=True)
    stage = p.dir.parent / f".restore-{p.dir.name}-{uuid.uuid4().hex[:8]}"
    try:
        try:
            zf = zipfile.ZipFile(src)
        except zipfile.BadZipFile:
            raise S.StratError("VALIDATION_FAILED", f"{a.file} is not a zip archive")
        with zf:
            m = _check_members(zf)
            stage.mkdir()
            for d in ("blobs/sha256", "blobs/.incoming", "exports"):
                (stage / d).mkdir(parents=True, exist_ok=True)
            for name, meta in sorted(m["members"].items()):
                dest = stage.joinpath(*name.split("/"))   # names are already strictly matched
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as f, open(dest, "xb") as out:
                    digest, size = _copy_hashed(f, out, meta["size"])
                if digest != meta["sha256"] or size != meta["size"]:
                    raise S.StratError("VALIDATION_FAILED", f"{name} fails its hash check", member=name)
        con = sqlite3.connect(stage / DB_MEMBER, isolation_level=None)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise S.StratError("VALIDATION_FAILED", "restored database fails integrity_check")
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if row and row[0] in S.MIGRATABLE:   # an older archive: migrate the restored copy forward
                row = (S.migrate(con),)
            if not row or row[0] != str(S.SCHEMA_VERSION):
                raise S.StratError("UNSUPPORTED_SCHEMA", f"database schema {row[0] if row else None!r}, this strat "
                                                         f"needs {S.SCHEMA_VERSION}")
            missing = sorted({r[0] for r in con.execute("SELECT sha256 FROM source_version")}
                             - {n.rsplit("/", 1)[1] for n in m["members"] if BLOB_MEMBER.match(n)})
            if missing:
                raise S.StratError("BLOB_MISSING", "archive lacks blobs its source versions refer to", blobs=missing)
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE lease SET owner_session=NULL, expires_at=NULL, fencing_token=fencing_token+1 "
                        "WHERE id=1")
            token = con.execute("SELECT fencing_token FROM lease WHERE id=1").fetchone()[0]
            revision = int(con.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
            con.execute("COMMIT")
        finally:
            con.close()
        if p.dir.exists():
            raise S.StratError("CONFLICT", f"project {p.dir.name} appeared meanwhile; nothing restored")
        stage.rename(p.dir)
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
    return "ok", {"project": p.dir.name, "from_project": m.get("project"), "schema_version": S.SCHEMA_VERSION,
                  "db_revision": revision, "fencing_token": token, "blobs": len(m["members"]) - 1,
                  "note": "the copy is a fork: take a new lease; old tokens are fenced. Run export to rebuild views"}


def register(sub):
    ap = sub.add_parser("archive")
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", required=True)
    rp = sub.add_parser("restore")
    rp.add_argument("--project", required=True)
    rp.add_argument("--file", required=True)


COMMANDS = {"archive": cmd_archive, "restore": cmd_restore}
