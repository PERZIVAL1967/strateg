# Agent core - build steps 1-6 (plan v1.3)

**For the IDE model:** start at [AGENTS.md](AGENTS.md) (Russian; `CLAUDE.md` imports it). It routes
each stage to a skill in `skills/`, and the skills link to `method/`. A new chat begins with
`python tools/strat.py context --project <id>`.

`tools/strat.py` is the only writer of a project's state (SQLite + content-addressed blobs);
`tools/strat_extract.py` turns stored bytes into addressable segments; `tools/strat_evidence.py` is
the evidence layer (observations, claims, records, one dependency graph, validators, revision). Python 3.9+ stdlib, no
server. Optional: `pip install -r requirements-optional.txt` for PDF (pinned `pypdf`).

```
python tools/strat.py init    --project demo
python tools/strat.py lease   --project demo --session chatA                       # -> fencing_token
python tools/strat.py context --project demo                                       # stage, next step, open items
python tools/strat.py allow   --project demo --session chatA --token 1 --run r1 --step 0 --pattern example.ru --reason "the human's words"
python tools/strat.py import  --project demo --session chatA --token 1 --run r1 --step 1 --file data.xlsx
python tools/strat.py fetch   --project demo --session chatA --token 1 --run r1 --step 2 --url https://example.ru/tariffs
python tools/strat.py extract --project demo --session chatA --token 1 --source-version SV-...
python tools/strat.py locate  --project demo --source-version SV-... --exact "500 руб." [--prefix ..] [--suffix ..] [--hint ..]
python tools/strat.py classify --project demo --session chatA --token 1 --source-version SV-... --source-kind vendor
python tools/strat.py observe  ... --run r1 --step 3 --source-version SV-... --exact "990 руб." --kind measurement
python tools/strat.py claim    ... --run r1 --step 4 --type fact --text "..." --scope '{"audience":..,"region":..,"period":..,"product":..,"metric":..}' --supports O-...
python tools/strat.py record   ... --run r1 --step 5 --kind option --title "..." --fields '{"role":"status_quo","objection":"..","reversal":".."}' --depends C-...
python tools/strat.py validate --project demo
python tools/strat.py impact   --project demo --ids O-...
python tools/strat.py revalidate ... --run r1 --step 9 --old SV-old --new SV-new
python tools/strat.py resolve ... --run r1 --step 10 --ids C-old --outcome supersede --by C-new --reason "..."
python tools/strat.py export  --project demo
python tools/strat.py status  --project demo
python tools/strat.py doctor  --project demo
python tools/strat.py cost    --project demo --session chatA --token 1 --run r1 --step 11 --stage strategy --kind model --amount 0.42 --unit usd --basis ide
python tools/strat.py search  --project demo --query "подключ" [--limit 20] [--include-history]
python tools/strat.py archive --project demo --out demo.zip
python tools/strat.py restore --file demo.zip --project demo2
python tools/strat.py read    --project demo --source-version SV-... [--around "990 руб."] [--chars 6000]
python tools/strat.py abandon --project demo --session chatA --token 1 --op op-... --reason "site gone; human saves the page"
python tools/strat.py record   ... --kind section --title "Часть 1" --fields '{"part":"1","body":"... 6 млн руб. [CAL-0123456789]"}' --depends CAL-...
```
Every command prints one JSON envelope: `status`, `error_code`, `retryable`, `data` - also on failure.

`--fields` and `--scope` take inline JSON or `@file.json` (PowerShell drops the inner quotes of inline JSON).

## Tests: `python -m unittest discover -s tests -v` (165 tests)
**Step 1 - state and recovery (25, `test_step1.py`)**
- Crash at 5 points (after pending, `.part`, receipt, canonical publish, commit) -> exactly one
  source version, bytes present, `.incoming/` clean. Receipts attached only on a full identity match;
  5 tamper cases + malformed JSON rejected; crashed attempt cost stays `unknown`.
- (run, step) is a pair; files named by generated op ids; colliding/look-alike keys stay distinct.
- `LEASE_HELD`, `FENCED`, paused writer -> `BUSY` (SQLite code kept), killed writer releases,
  same-session race -> one version; other schema versions -> `UNSUPPORTED_SCHEMA`.

**Step 2 - sources (39, `test_step2.py`)**
- `fetch`: http/https only, no embedded credentials; every address (first URL **and each redirect**)
  must be public at resolve time - private, loopback, link-local (incl. 169.254.169.254), IPv6 loopback
  refused before any request (this is a resolve-time check, not connection-level enforcement); system
  and environment proxies are never used; max 5 redirects; 25 MB; **one 20 s budget for the whole
  download** (each redirect hop gets only the remaining budget; a trickling body or a slow redirect chain times out); HTTP errors typed (404 `NOT_FOUND` as a failed
  attempt, retry = next attempt); crash after receipt -> resume without a second download.
- Extractors: text/Markdown (heading as context), CSV (delimiter sniffed; cell context = column +
  row), HTML (server-rendered only; JS shell -> `blocked:dynamic`; table cells carry table number,
  caption, column header and row label, colspan cells emitted once with all their headers, tables with
  rowspan -> `partial`), DOCX (paragraphs, headings,
  tables), XLSX (shared strings, inline strings; **formula without cached value -> unknown, never
  computed**; merged ranges recorded; number formats not interpreted), PDF (per-page coverage; page
  without text layer reported; all pages empty -> `blocked:scanned`; no OCR). Zip members with
  `..`/absolute paths and oversized archives refused. Unknown formats -> `blocked:unsupported_format`.
- `locate`: text-quote selector over segments - **unique match or explicit `not_found`/`ambiguous`
  with candidates, never a guess**; prefix/suffix/hint disambiguate; returns span + context hashes.
- `export` is a **view export**: Markdown + deterministic JSONL dump (segments included) +
  `manifest.json` with content hashes and the DB revision, written last under an OS lock, so two exports
  never interleave and a crash mid-export is never reported `current`. `doctor` reports `current`,
  `stale` (DB moved on) or `edited` (bytes differ). It is **not** the portable archive (DB snapshot +
  pinned blobs + GC guard) - that is not implemented yet.
- CLI output is UTF-8 bytes whatever the console encoding (tested under cp1252).

**Step 3 - evidence (24, `test_step3.py`)** - a small eSIM case (operator tariff page + app reviews)
-> observations -> claims -> 3 options -> capability -> strategy -> message, then each gate broken:
- `observe` only accepts a quote that resolves uniquely (else `UNRESOLVED` with candidates); every write
  is idempotent per (run, step) and written together with its operation row in one transaction.
- **Gates** (structural, must pass): fact without evidence; fact whose evidence is all superseded/under
  review; missing scope fields (`unknown: reason` allowed); **demand stated as fact on search/review/
  forum/synthetic/unclassified sources only**; fact resting on a synthetic claim; record without basis;
  dangling edge; dependency cycle; < 3 options or no `status_quo`/`closure` option; option without
  objection or reversal condition; message without our own capability (vendor claims can't back it);
  planned capability promised as current.
- **Flags** (heuristics for human review, never proofs): future wording in a fact; `unknown` scope
  without reason; records/claims under review.
- `impact`: everything resting on given ids, transitively - and nothing that doesn't.
- `revalidate`: new source version -> observations carried forward only if the quote resolves uniquely
  (recorded hint first) with the same span **and** context hash; else `review_required` with a reason
  (`not_found`, `ambiguous`, `span_changed`, `context_changed`) + review events for everything resting
  on it. Fixtures: footer-only edit -> all carried; changed heading -> review; byte-identical cell
  under a changed column header -> `context_changed`.
- `export` adds `claims.md`: each claim with type, status, scope and full citations (quote, source,
  location, context, retrieval date, source kind).
**Step 4 - the IDE-facing layer (39, `test_step4.py`)**
- **Source allowlist** (brief: «из разрешённых источников»): `fetch` only reaches hosts the human
  allowed (`allow --reason` records their words; `--covers host|subdomains` is required, so the human
  sees whether a "yes" is one exact site or the site with every subdomain; Cyrillic domains compared in
  IDNA form). The allowlist limits where `fetch` goes; it does not make an allowed page trustworthy -
  injected text on an allowed host is still data, and `source_kind` is provenance, not reliability. It is checked before DNS for the first URL and **every redirect**; an empty
  list refuses everything and leaves no operation behind; `--revoke` stops new fetches and keeps history.
  Local `import` is the human's own choice of file and is not filtered.
- **`context`** (resume in a new chat, C01/C02): read-only, one snapshot. The stage is computed from
  the DB by fixed rules (`resume_operation` > `framing` > `collecting` > `researching` > `options` >
  `strategy` > `review_required` (after a decision) > `fixing` > `awaiting_decision` > `decided`),
  with the skill to read, the next free step per run, unfinished operations, failed gates, items under
  review, open questions, decisions with the human's words, commitments, and a hash of the
  instructions + method files. Two chats reading the same DB get the same answer. Also exported as
  `exports/context.md`.
- **Gates from the brief's minimum**: frame needs goal/recipient/constraints/data; product strategy
  needs audience/job/alternatives/direction/refusals/economics; comms strategy needs
  audience/situation/barrier/key_message/proof/channels **and must rest on a product strategy**; a
  test plan needs expected change/baseline/period/side effects; a decision needs `actor: human`, the
  human's words, and an option it chooses.
- **From Codex's step-3 review (R035):** "a fact can't rest on synthetic material" now follows the
  whole chain (depends + supports, not contradicts) and also catches observations from a
  synthetic-kind source; a capability marked `current` must rest on a fact claim; `validate` reports
  `structural_passed` with a note that gates don't certify meaning or real demand.
- **`resolve`** closes a review: `reaffirm`, `supersede --by NEW` or `withdraw`, always with a reason;
  nothing is deleted; superseded/withdrawn facts are history and no longer judged by the fact gates.
  Fixture: price change -> new fact supersedes the old -> the rest reaffirmed -> new human decision
  supersedes the old -> stage `decided`, gates pass.
- **From Codex's step-4 review (R037): current work vs history.** Superseded and withdrawn items
  (records now have their own `withdrawn` status) are history: `context` shows them under `history`,
  with how and by what they were retired and a decision's original words, but never counts them. The
  stage needs a *current* decision for `decided`, current options/strategies/experiments, and exactly
  one current frame (two -> `FRAME_CONFLICT`, never a silent pick); two current decisions ->
  `DECISION_CONFLICT`. A decision names its option (`fields.option`, also a dependency); a retired
  choice -> `DECISION_OPTION_RETIRED`. A `current` capability needs a *current* fact. Anything still
  resting on history gets the `RESTS_ON_RETIRED` flag. `resolve` only touches current items; a
  replacement must be current, not itself, and the same kind (claim/claim, same record kind, same
  strategy type, frame/frame), so supersession can't loop. A decision is never reaffirmed by a reason:
  only a new human decision supersedes it, and withdrawing it needs the human's words (`--answer`).
  `revalidate` no longer pulls history back into `review_required`.
- **Instructions stay in sync with the CLI**: every `strat.py <command>` named in AGENTS.md and the
  skills exists; every stage's skill exists with `name`/`description` front matter; every linked
  method file exists.
- Instructions (Russian): `AGENTS.md`, 7 skills (`strat-resume`, `-frame`, `-collect`, `-research`,
  `-options`, `-strategy`, `-revisit`), method notes (`research`, `sources`, `options`, `strategy`).

**Step 5 - cost, report, portability, search (20, `test_step5.py`)**
- **`cost`** (brief: the user sees what the work cost): a ledger the agent writes, because the tool
  can't see model spend. Each entry: stage, kind (`model|search|tool|human`), amount >= 0, unit
  (`usd|rub|tokens|minutes`) and basis (`ide` = the number the IDE showed, `measured`, `estimate`).
  Lease-fenced and idempotent per (run, step) like every write. `context` gains a `cost` block:
  totals per kind+unit+basis, `model_cost_logged`, and the kinds never logged under `unknown` -
  absence is never shown as zero. Also counts failed/repeated operation attempts.
- **`exports/report.md`**: one Russian document a product manager reads without the chat - task,
  facts with numbered source references, vendor claims, hypotheses/forecasts/synthetic (labelled as not
  proving demand), unknowns (open questions + `unknown: ...` values), contradictions, options with all
  their fields, product and comms strategy with each message's capability and readiness, test plan,
  the decision with the human's words, commitments and items under review, cost, numbered sources.
  Current items only; an empty section says «Ещё не сделано: ...» instead of disappearing. Written in
  the same export transaction and manifest as the other files. Long dashes are replaced by hyphens;
  exact quotes stay in `claims.md`.
- **`archive` / `restore`**: a zip with a consistent DB snapshot (SQLite backup API), every blob a
  source version refers to, and a manifest (sha256 + size per member, schema version). Archive is
  read-only, holds the export lock, refuses if a blob is missing or damaged or the output file exists,
  clears the lease owner in the copy only and re-verifies every hash after writing. Restore goes into a
  **new** project only; member names must match strict patterns and the manifest exactly (no `..`,
  absolute paths, backslashes or extra members), blobs must be named by their hash, total size <= 2 GB,
  every hash, the schema version and `integrity_check` are verified; it unpacks into a staging folder
  renamed into place at the end, so a failure leaves no project behind. The copy is a fork: no lease
  owner and the fencing token is incremented, so an old chat's token is fenced. Exports are views and
  are not archived (run `export`). Nothing deletes blobs.
- **`search`**: read-only; a temporary FTS5 index built at query time (nothing persisted, never
  stale) over claim text, observation quote/value/unit and record title + fields. A `unicode61`
  (words) and a `trigram` (substrings - helps with Russian word forms, not morphology) index, merged;
  every user term is quoted, so FTS syntax characters are plain text. Without FTS5 it falls back to a
  case-insensitive substring scan, and `mode` says which ran. Claim hits list their supporting
  observations and source versions. History is excluded unless `--include-history`.

**Step 6 - final document + rehearsal fixes (18, `test_step6.py`), schema v8**
- New record kinds for `exports/document.md`: `section` (part `summary|1..5|comms`, body), `calc` (script
  under `projects/<id>/work/`, outputs, assumptions), `forecast_row` (metric, no_change, ours = number or
  low/base/high, driver), `quarter` (q 1-4, known, decision), `resource`, `risk`, `ask` (when Q1|Q2),
  `target`. Missing fields -> `<KIND>_FIELD_MISSING` gate. A new current section / forecast row / quarter /
  ask with the same part / metric / q / when supersedes the old one (review event, shown in history).
  Experiments also need unit, assignment, itt, sample_size, recruitment, threshold, stop_rule.
- Flags: `CALC_SCRIPT_MISSING`, `DOC_PART_MISSING` (only once a section exists),
  `SECTION_NUMBER_WITHOUT_SOURCE` (a paragraph with digits and no `[ID]`), `SECTION_REF_UNKNOWN`.
- Stage `documenting` (skill `strat-document`) after strategy + experiment, before `awaiting_decision`,
  while any section part is missing; `context.open.missing_sections`.
- `export` writes `document.md` (summary, parts 1-5, comms; tables for forecast rows, quarters, target,
  people, risks, asks; calcs under the part whose section depends on them, else part 3); `report.md`
  is its appendix.
- Fixes from the 11 Nov rehearsal: CA bundle fallback (`SSL_CERT_FILE` unset -> certifi, else
  `/etc/ssl/cert.pem`; verification stays on; `doctor.tls_ca_source`; cert errors -> non-retryable
  `TLS`); a fetched body with < 200 characters of text -> `BLOCKED` / `empty_body`, nothing stored; a
  non-retryable failure closes the op as `failed` (listed in `context.open.failed_operations`, never
  waited on; rerunning the same run/step retries); `abandon` closes a stuck op; `read` shows the text;
  demand is a fact only on `statistics` / `research` sources (allowlist; vendor no longer counts).
- Schema 7 projects and archives are migrated forward on open (record table rebuilt); older ones are refused.

Mutation check: disabling the demand gate, the context-hash comparison, or the transitive walk each
fails at least one step-3 test. For R037, ten mutants (context counting history, silent frame pick,
retired fact as evidence, kind-mismatched replacement, reaffirmed decision, withdrawal without the
human's words, revalidate reviving history, exact host matching subdomains, implicit option, two
current decisions) each fail at least one test.

Verified: macOS / Python 3.13 / pypdf 6.21.2 - 127/127. Step 1 (25 tests) verified unchanged on
Windows / Python 3.12 by Codex (R023, R025); step 2 first Windows run (R027) found the encoding,
HTML-context, deadline and export issues; the fixed 63-test version passed unchanged on Windows (R029: with pypdf 63/63; without it 61 passed + 2 PDF skips); test 64 (redirect budget) passed in the affected-subset run (R031). Step 3 subset passed on Windows (R035); the full step-4 suite passed unchanged on Windows, 116/116 (R037). **The R037 fixes (127 tests) not yet run on Windows.** Step 5 (147 tests) verified on macOS only.
Limits (stated, not hidden): process-crash recovery, not power-loss durability; DNS rebinding between
the address check and the connection is not prevented; DNS lookup time itself can't be bounded from
Python; `STRAT_TEST_ALLOW_LOOPBACK=1` unlocks
127.0.0.1 only, for tests, and `doctor` shows it. Stated limit: `actor: human` and the quoted answer are recorded, not proven - the tool can't tell who
typed them; the chat transcript is the evidence. Not yet: GC + maintenance guard (by design nothing deletes blobs); model cost is not measured by the tool
(the agent logs what its IDE shows with `cost --basis ide`, or nothing - then it is "unknown"). Schema is now
v8 (document record kinds); v7 projects migrate forward automatically, older ones are refused with `UNSUPPORTED_SCHEMA`.
