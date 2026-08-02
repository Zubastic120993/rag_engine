#!/usr/bin/env python3
"""
verify_rag_findings.py — read-only verification of four open evidence gaps
in the rag_engine audit (session 1 of the repair sequence).

RUN WITH THE PROJECT VENV:
    cd ~/CE_Library/Tools/rag_engine
    ./venv/bin/python3 verify_rag_findings.py | tee verify_findings_$(date +%Y%m%d_%H%M).txt

WHAT IT DOES
    C1  Distinct-source path census (replaces the 500-chunk-weighted sample)
    C2  F-03 milder case — what --scope actually consumes from question text
    C3  F-04 multi-revision screen — are two revisions of one document indexed?
    C4  Denominator reconciliation — 1,673 digests vs 1,599 sources vs 74 empties

WHAT IT DOES NOT DO
    No writes to Chroma. No writes to the tracker. No writes to the library.
    No ingest, no delete, no upsert, no reconcile. No LLM calls.
    Output goes to stdout only; redirect it yourself if you want a file.

HONEST CAVEAT
    chromadb has no read-only open mode. Opening a PersistentClient touches
    sqlite WAL/journal files at the filesystem level even though no row is
    modified. This is not a data mutation, but it is not a frozen-file read
    either. Have your snapshot in place before running, as you would anyway.

EVIDENCE RULE
    Every number printed is followed by how it was obtained. Anything that
    cannot be determined prints UNRESOLVED with the reason — it is never
    inferred, defaulted, or filled in.
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------- utilities

RESULTS = []  # (check_id, status, one_line_summary)


def record(check_id, status, summary):
    RESULTS.append((check_id, status, summary))


def hdr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def sub(title):
    print("\n--- " + title)


def unresolved(check_id, reason):
    print(f"\n  UNRESOLVED: {reason}")
    record(check_id, "UNRESOLVED", reason)


def run(cmd, cwd=None):
    """Run a command, return (rc, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=60
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        return -1, "", f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------ environment

hdr("ENVIRONMENT ANCHOR (results are only valid against this state)")

REPO = Path(__file__).resolve().parent
print(f"  script location      : {REPO}")
print(f"  python executable    : {sys.executable}")
print(f"  python version       : {sys.version.split()[0]}")

rc, out, _ = run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO)
print(f"  git HEAD             : {out if rc == 0 else 'UNRESOLVED (not a git repo?)'}")
rc, out, _ = run(["git", "status", "--porcelain"], cwd=REPO)
if rc == 0:
    dirty = [ln for ln in out.splitlines() if ln.strip()]
    print(f"  working tree         : {'DIRTY (' + str(len(dirty)) + ' entries)' if dirty else 'clean'}")
else:
    print("  working tree         : UNRESOLVED")

# --- library root: prefer the engine's own config, never hardcode silently
LIBRARY_ROOT = None
ROOT_SOURCE = None
try:
    sys.path.insert(0, str(REPO))
    from rag_engine import config as rg_config  # type: ignore

    for attr in ("library_root", "LIBRARY_ROOT", "get_library_root"):
        if hasattr(rg_config, attr):
            val = getattr(rg_config, attr)
            LIBRARY_ROOT = Path(val() if callable(val) else val).expanduser().resolve()
            ROOT_SOURCE = f"rag_engine.config.{attr}"
            break
except Exception as exc:  # noqa: BLE001
    print(f"  (rag_engine.config import failed: {type(exc).__name__}: {exc})")

if LIBRARY_ROOT is None:
    print("  library root         : UNRESOLVED — could not obtain from rag_engine.config")
    print("                         Set it explicitly and re-run:")
    print("                         RAG_VERIFY_ROOT=~/CE_Library ./venv/bin/python3 verify_rag_findings.py")
    env_root = os.environ.get("RAG_VERIFY_ROOT")
    if env_root:
        LIBRARY_ROOT = Path(env_root).expanduser().resolve()
        ROOT_SOURCE = "RAG_VERIFY_ROOT env override"

if LIBRARY_ROOT:
    print(f"  library root         : {LIBRARY_ROOT}   [via {ROOT_SOURCE}]")
    print(f"  library root exists  : {LIBRARY_ROOT.is_dir()}")

# --- persist dir
PERSIST_DIR = None
try:
    if hasattr(rg_config, "persist_dir"):
        PERSIST_DIR = Path(rg_config.persist_dir()).expanduser().resolve()
    elif hasattr(rg_config, "PERSIST_DIR"):
        PERSIST_DIR = Path(rg_config.PERSIST_DIR).expanduser().resolve()
except Exception:  # noqa: BLE001
    pass
print(f"  chroma persist dir   : {PERSIST_DIR if PERSIST_DIR else 'UNRESOLVED'}")

# --- open chroma (read path only)
COLLECTION = None
CHROMA_COUNT = None
try:
    import chromadb  # type: ignore

    print(f"  chromadb version     : {chromadb.__version__}")
    if PERSIST_DIR and PERSIST_DIR.is_dir():
        client = chromadb.PersistentClient(path=str(PERSIST_DIR))
        names = [c.name for c in client.list_collections()]
        print(f"  collections          : {names}")
        if len(names) == 1:
            COLLECTION = client.get_collection(names[0])
        elif "langchain" in names:
            COLLECTION = client.get_collection("langchain")
            print("  NOTE: multiple collections present; using 'langchain'")
        if COLLECTION is not None:
            CHROMA_COUNT = COLLECTION.count()
            print(f"  chunk count          : {CHROMA_COUNT:,}   [collection.count()]")
except Exception as exc:  # noqa: BLE001
    print(f"  chroma open          : UNRESOLVED — {type(exc).__name__}: {exc}")

# --- tracker
TRACKER_PATH = None
TRACKER = None
for cand in ("embedded.json",):
    for base in filter(None, [PERSIST_DIR, LIBRARY_ROOT, REPO]):
        p = Path(base) / cand
        if p.is_file():
            TRACKER_PATH = p
            break
    if TRACKER_PATH:
        break
if TRACKER_PATH is None and LIBRARY_ROOT and LIBRARY_ROOT.is_dir():
    hits = list(LIBRARY_ROOT.rglob("embedded.json"))
    if len(hits) == 1:
        TRACKER_PATH = hits[0]
    elif len(hits) > 1:
        print(f"  tracker              : AMBIGUOUS — {len(hits)} embedded.json found:")
        for h in hits:
            print(f"                           {h}")

print(f"  tracker path         : {TRACKER_PATH if TRACKER_PATH else 'UNRESOLVED'}")
if TRACKER_PATH:
    try:
        TRACKER = json.loads(TRACKER_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"  tracker load         : UNRESOLVED — {type(exc).__name__}: {exc}")


def find_entries(obj, depth=0):
    """Locate the dict-of-entries containing chunk_ids. Shape is not assumed."""
    if depth > 3 or not isinstance(obj, dict):
        return None
    vals = [v for v in obj.values() if isinstance(v, dict)]
    if vals and sum(1 for v in vals if "chunk_ids" in v) >= max(1, len(vals) // 2):
        return obj
    for v in obj.values():
        found = find_entries(v, depth + 1)
        if found is not None:
            return found
    return None


ENTRIES = find_entries(TRACKER) if isinstance(TRACKER, dict) else None
if TRACKER is not None and ENTRIES is None:
    print("  tracker shape        : UNRESOLVED — no dict-of-entries with 'chunk_ids' found")
    print(f"                         top-level type={type(TRACKER).__name__}, "
          f"keys sample={list(TRACKER)[:5] if isinstance(TRACKER, dict) else 'n/a'}")


def entry_path(e):
    """Extract the source path from a tracker entry without assuming the key."""
    for k in ("path", "source", "rel_path", "relpath", "file", "source_path"):
        if isinstance(e, dict) and isinstance(e.get(k), str):
            return e[k]
    return None


def resolve_source(s):
    """Metadata source -> absolute Path. Handles absolute and relative forms."""
    p = Path(s)
    if p.is_absolute():
        return p
    return (LIBRARY_ROOT / s) if LIBRARY_ROOT else None


# =========================================================== C1: path census

hdr("C1 — DISTINCT-SOURCE PATH CENSUS  (replaces the 500-chunk sample in R2)")

CHROMA_SOURCES = None
if COLLECTION is None:
    unresolved("C1", "Chroma collection not open")
else:
    sub("Paging all chunk metadata (read-only collection.get)")
    counts = defaultdict(int)
    missing_meta = 0
    offset, page = 0, 10000
    try:
        while True:
            batch = COLLECTION.get(limit=page, offset=offset, include=["metadatas"])
            metas = batch.get("metadatas") or []
            if not metas:
                break
            for m in metas:
                src = (m or {}).get("source")
                if isinstance(src, str) and src:
                    counts[src] += 1
                else:
                    missing_meta += 1
            offset += len(metas)
            print(f"    ... {offset:,} chunks scanned", flush=True)
            if len(metas) < page:
                break
        CHROMA_SOURCES = counts

        total_scanned = sum(counts.values()) + missing_meta
        print(f"\n  chunks scanned            : {total_scanned:,}")
        if CHROMA_COUNT is not None:
            delta = CHROMA_COUNT - total_scanned
            print(f"  collection.count()        : {CHROMA_COUNT:,}")
            print(f"  scan vs count delta       : {delta:,}"
                  f"{'   <-- INVESTIGATE, paging lost rows' if delta else '   (consistent)'}")
        print(f"  chunks with no 'source'   : {missing_meta:,}")
        print(f"  DISTINCT sources          : {len(counts):,}")

        sub("Stat'ing every distinct source (census, not sample)")
        dead, alive = [], 0
        abs_form = sum(1 for s in counts if Path(s).is_absolute())
        print(f"  path form: {abs_form:,} absolute / {len(counts) - abs_form:,} relative")
        if LIBRARY_ROOT is None and abs_form < len(counts):
            unresolved("C1", "relative source paths present but library root UNRESOLVED")
        else:
            for s in counts:
                p = resolve_source(s)
                if p is not None and p.exists():
                    alive += 1
                else:
                    dead.append(s)
            print(f"\n  resolvable on disk        : {alive:,} / {len(counts):,}")
            print(f"  DEAD paths                : {len(dead):,}")
            if dead:
                print("\n  Dead path list (chunk count, path):")
                for s in sorted(dead, key=lambda x: -counts[x]):
                    print(f"    {counts[s]:>7,}  {s}")
                record("C1", "FAIL", f"{len(dead)} dead paths across {len(counts)} distinct sources")
            else:
                record("C1", "PASS",
                       f"0 dead paths, census of {len(counts)} distinct sources "
                       f"(previous evidence was a 500-chunk sample)")

            sub("Chunk-count concentration (why the old sample over-weighted)")
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:10]
            for s, n in top:
                share = (n / sum(counts.values())) * 100
                print(f"    {n:>7,}  ({share:5.2f}% of corpus)  {s}")
    except Exception as exc:  # noqa: BLE001
        unresolved("C1", f"{type(exc).__name__}: {exc}")


# ====================================================== C2: F-03 milder case

hdr("C2 — F-03 MILDER CASE  (does --scope silently eat a question word?)")

print("""
  The audit claimed this but pasted no output, violating its own evidence
  rule. Parser-level introspection is used here rather than a live `ask`
  invocation: it shows exactly what argparse consumed, costs no LLM call,
  and mutates nothing.
""")

parser = None
parser_origin = None
try:
    from rag_engine import cli as rg_cli  # type: ignore

    for attr in ("build_parser", "_build_parser", "make_parser", "get_parser", "create_parser"):
        if hasattr(rg_cli, attr):
            try:
                parser = getattr(rg_cli, attr)()
                parser_origin = f"rag_engine.cli.{attr}()"
                break
            except Exception:  # noqa: BLE001
                continue
except Exception as exc:  # noqa: BLE001
    print(f"  cli import failed: {type(exc).__name__}: {exc}")

if parser is None:
    unresolved("C2",
               "no parser factory found in rag_engine.cli "
               "(tried build_parser/_build_parser/make_parser/get_parser/create_parser)")
    print("""
  MANUAL FALLBACK — run these four by hand and paste stdout+stderr+exit code:

    ./venv/bin/rag-engine ask what does the scope wiki mean --json ; echo "exit=$?"
    ./venv/bin/rag-engine ask what does --scope wiki mean --json   ; echo "exit=$?"
    ./venv/bin/rag-engine ask explain -k 5 exhaust valve timing    ; echo "exit=$?"
    ./venv/bin/rag-engine ask compare --scope wiki and manuals     ; echo "exit=$?"

  The finding to watch for is NOT the crash (already documented). It is any
  case that exits 0 having silently answered a DIFFERENT question under a
  DIFFERENT scope than the operator typed. That is a silent wrong result
  against a silently changed scope = S1 by the report's own severity table.
""")
else:
    print(f"  parser obtained via: {parser_origin}\n")
    cases = [
        ["ask", "what", "does", "--scope", "wiki", "mean", "--json"],
        ["ask", "explain", "-k", "5", "exhaust", "valve", "timing"],
        ["ask", "compare", "--scope", "wiki", "and", "manuals"],
        ["ask", "what", "does", "scope", "wiki", "mean", "--json"],
    ]
    silent_divergence = 0
    for argv in cases:
        print("  argv     : " + " ".join(argv))
        try:
            ns = parser.parse_args(argv)
            d = vars(ns)
            q = d.get("question")
            q = " ".join(q) if isinstance(q, list) else q
            scope = d.get("scope")
            k = d.get("k")
            print(f"  question : {q!r}")
            print(f"  scope    : {scope!r}    k: {k!r}")
            typed = " ".join(a for a in argv[1:] if not a.startswith("-"))
            if q is not None and q.strip() != typed.strip():
                print(f"  >>> DIVERGENCE: operator typed {typed!r}, engine will answer {q!r}")
                silent_divergence += 1
            else:
                print("  (question text preserved)")
        except SystemExit as e:
            print(f"  result   : argparse SystemExit(code={e.code}) — no Namespace produced")
            print("             (loud failure; this is the already-documented F-03 crash)")
        print()
    if silent_divergence:
        record("C2", "FAIL",
               f"{silent_divergence} case(s) silently altered the question and/or scope "
               f"-> F-03 is S1, not S3")
    else:
        record("C2", "PASS",
               "no silent question/scope divergence reproduced at parser level; "
               "F-03 stays S3 (crash-only)")


# =================================================== C3: F-04 revision screen

hdr("C3 — F-04 MULTI-REVISION SCREEN  (are two revisions of one doc indexed?)")

REV_TOKENS = [
    re.compile(r"[_\-\s]r\s?\d{1,3}(?=[_\-\s.]|$)", re.I),
    re.compile(r"[_\-\s]rev\.?\s?\d{1,3}", re.I),
    re.compile(r"[_\-\s]v\s?\d{1,3}(\.\d{1,3})?(?=[_\-\s.]|$)", re.I),
    re.compile(r"[_\-\s]iss(ue)?\.?\s?\d{1,3}", re.I),
    re.compile(r"[_\-\s]ed(ition)?\.?\s?\d{1,3}", re.I),
]
DATE_TOKEN = re.compile(r"[_\-\s](20\d{6}|\d{6}|20\d{2})(?=[_\-\s.]|$)")


def strip_ext(name):
    prev = None
    while prev != name:
        prev = name
        for ext in (".pdf", ".md", ".txt", ".docx"):
            if name.lower().endswith(ext):
                name = name[: -len(ext)]
    return name


def normalise(stem):
    s = strip_ext(stem)
    has_rev = any(rx.search(s) for rx in REV_TOKENS)
    for rx in REV_TOKENS:
        s = rx.sub(" ", s)
    s = DATE_TOKEN.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return s, has_rev


if not CHROMA_SOURCES:
    unresolved("C3", "distinct source list unavailable (C1 did not complete)")
else:
    same_dir = defaultdict(list)
    stem_only = defaultdict(list)
    rev_marked = []
    for s in CHROMA_SOURCES:
        p = Path(s)
        norm, has_rev = normalise(p.name)
        if not norm:
            continue
        same_dir[(str(p.parent), norm)].append(s)
        stem_only[norm].append(s)
        if has_rev:
            rev_marked.append(s)

    print(f"  distinct sources screened        : {len(CHROMA_SOURCES):,}")
    print(f"  sources carrying a revision token: {len(rev_marked):,}")
    if rev_marked:
        print("\n  Sample of revision-marked sources (up to 15):")
        for s in sorted(rev_marked)[:15]:
            print(f"    {s}")

    g1 = {k: v for k, v in same_dir.items() if len(v) > 1}
    sub(f"GROUP A — same directory, same normalised stem: {len(g1)} group(s)")
    if g1:
        for (parent, norm), members in sorted(g1.items()):
            print(f"\n  dir  : {parent}")
            print(f"  stem : {norm!r}")
            for m in sorted(members):
                print(f"      [{CHROMA_SOURCES[m]:>6,} chunks]  {Path(m).name}")
    else:
        print("  none")

    g2 = {k: v for k, v in stem_only.items()
          if len(v) > 1 and not any(set(v) <= set(mv) for mv in g1.values())}
    sub(f"GROUP B — same normalised stem, different directories: {len(g2)} group(s)")
    if g2:
        for norm, members in sorted(g2.items()):
            print(f"\n  stem : {norm!r}")
            for m in sorted(members):
                print(f"      [{CHROMA_SOURCES[m]:>6,} chunks]  {m}")
    else:
        print("  none")

    print("""
  READ THIS AS A SCREEN, NOT A VERDICT.
  A group is a candidate for human review. Translations, volume splits, and
  coincidental stem collisions all land here. Two revisions of one document
  both being indexed is confirmed by opening them, not by this script.
""")
    if g1 or g2:
        record("C3", "FAIL",
               f"{len(g1)} same-dir + {len(g2)} cross-dir candidate groups "
               f"-> F-04 cannot stay 'no live exposure' until reviewed")
    else:
        record("C3", "PASS",
               f"no duplicate-stem groups among {len(CHROMA_SOURCES):,} sources; "
               f"F-04 'can wait' is supported")


# ================================================ C4: denominator reconciliation

hdr("C4 — DENOMINATOR RECONCILIATION  (1,673 digests vs 1,599 sources vs 74 empty)")

if ENTRIES is None:
    unresolved("C4", "tracker entries not located")
else:
    n_digests = len(ENTRIES)
    empties, nonempty, chunk_sum = [], 0, 0
    tracker_paths = set()
    no_path = 0
    for digest, e in ENTRIES.items():
        ids = e.get("chunk_ids") if isinstance(e, dict) else None
        if isinstance(ids, list):
            chunk_sum += len(ids)
            if len(ids) == 0:
                empties.append(digest)
            else:
                nonempty += 1
        pth = entry_path(e)
        if pth:
            tracker_paths.add(pth)
        else:
            no_path += 1

    print(f"  tracker digests                : {n_digests:,}")
    print(f"  digests with chunk_ids == []   : {len(empties):,}")
    print(f"  digests with chunk_ids  > 0    : {nonempty:,}")
    print(f"  distinct paths in tracker      : {len(tracker_paths):,}")
    print(f"  entries with no path key       : {no_path:,}")
    print(f"  sum(len(chunk_ids))            : {chunk_sum:,}")

    sub("Hypothesis under test: digests - sources == empty-chunk digests")
    if CHROMA_SOURCES is not None:
        n_sources = len(CHROMA_SOURCES)
        diff = n_digests - n_sources
        residual = diff - len(empties)
        print(f"    {n_digests:,} digests - {n_sources:,} chroma sources = {diff:,}")
        print(f"    empty-chunk digests                       = {len(empties):,}")
        print(f"    RESIDUAL                                  = {residual:,}")
        if residual == 0:
            print("\n    CONFIRMED. The gap is exactly the empty-extraction set (F-02).")
            record("C4", "PASS", "denominator gap fully explained by empty-chunk digests")
        else:
            print(f"\n    NOT CONFIRMED. {abs(residual):,} digest(s) unaccounted for.")
            print("    Do not proceed to F-01 cleanup until this residual is named —")
            print("    an unexplained digest population is exactly the shape of the")
            print("    orphan problem F-01 is meant to close.")
            record("C4", "FAIL", f"residual of {residual} digests unexplained")
    else:
        unresolved("C4", "chroma distinct-source count unavailable")

    sub("Cross-check: tracker chunk total vs Chroma chunk total (F-01 baseline)")
    if CHROMA_COUNT is not None:
        orphan = CHROMA_COUNT - chunk_sum
        print(f"    collection.count()      = {CHROMA_COUNT:,}")
        print(f"    sum(len(chunk_ids))     = {chunk_sum:,}")
        print(f"    ORPHANED CHUNKS         = {orphan:,}")
        print(f"    (audit reported 3,193 — {'MATCHES' if orphan == 3193 else 'DIVERGED, corpus changed since the audit'})")

    if empties:
        sub(f"Empty-extraction digests ({len(empties):,}) — the F-02 population")
        shown = 0
        for digest in empties:
            pth = entry_path(ENTRIES[digest])
            print(f"    {pth if pth else '(no path key)  digest=' + str(digest)[:16]}")
            shown += 1
            if shown >= 80:
                print(f"    ... and {len(empties) - shown:,} more")
                break


# ======================================================================= end

hdr("SUMMARY")

if not RESULTS:
    print("  No check completed. Fix the environment anchor above and re-run.")
else:
    width = max(len(r[1]) for r in RESULTS)
    for cid, status, summary in RESULTS:
        print(f"  {cid}  {status:<{width}}  {summary}")

print("""
NEXT STEP GATE
  C4 must read PASS before any F-01 cleanup is planned. An unexplained
  digest residual means the tracker is not a trustworthy basis for deciding
  which chunk ids are orphans — and F-01's repair deletes chunks on exactly
  that basis.

  C2 FAIL reorders the plan: F-03 becomes S1 and moves ahead of F-05.
  C3 FAIL removes F-04 from 'can wait'.

  Nothing was written. First code change remains F-07.
""")
