"""Reproducible tool-chain evaluation on generated documents, never production files.

Run with the project's configured Python: ``python scripts/evaluate.py --output report.json``.
No LLM answer generation is performed. Optional retrieval configuration can call
an explicitly configured embedding/reranking service using synthetic text only.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import platform
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "python"))
from team_drive.engine import Engine


DOCUMENTS = {
    "policies/expense-approved.md": "# Expense allowance\nStatus: approved; effective 2026-01-01.\nClient meals allowance: CNY 120 per person.\nException: alcohol is excluded.\n",
    "drafts/expense-draft.md": "# Expense allowance\nStatus: draft; not effective.\nClient meals allowance: CNY 180 per person.\n",
    "policies/leave.md": "# Annual leave policy\n年假结转：最多五天，必须在三月底前使用。\n",
    "contracts/termination.md": "# Termination notice\nTermination requires 30 calendar days of notice.\nException: material breach allows immediate termination.\n",
    "finance/april.csv": "id,currency,amount,status\nA,CNY,100.10,approved\nB,CNY,200.20,approved\nC,USD,999.99,approved\nD,CNY,500.00,draft\n",
    "finance/may.csv": "id,currency,amount,status\nE,CNY,50.05,approved\nF,CNY,49.95,approved\n",
    "notes/changing.md": "Revision one: original evidence.\n",
    "notes/long.md": "".join("Evidence row %d: preserved context.\n" % i for i in range(1, 46)),
}
RETRIEVAL_CASES = [
    {"id": "expense_variants", "query": "expense allowance", "relevant_paths": ["policies/expense-approved.md", "drafts/expense-draft.md"]},
    {"id": "approved_scope", "query": "client meals", "prefix": "policies", "relevant_paths": ["policies/expense-approved.md"]},
    {"id": "chinese_leave", "query": "年假结转", "relevant_paths": ["policies/leave.md"]},
    {"id": "termination", "query": "termination notice", "relevant_paths": ["contracts/termination.md"]},
    {"id": "currency_scope", "query": "CNY approved", "prefix": "finance", "relevant_paths": ["finance/april.csv", "finance/may.csv"]},
]


def ranking_metrics(returned_paths, relevant_paths, k):
    """File-level Recall@K and reciprocal rank; empty gold is not a positive query."""
    if not isinstance(k, int) or isinstance(k, bool) or k < 1:
        raise ValueError("k must be a positive integer")
    relevant = set(relevant_paths)
    if not relevant:
        raise ValueError("Positive retrieval cases need at least one relevant file")
    ranked = list(dict.fromkeys(returned_paths))[:k]
    hits = relevant.intersection(ranked)
    ranks = [i for i, path in enumerate(ranked, 1) if path in relevant]
    return {"recall_at_k": len(hits) / len(relevant),
            "reciprocal_rank": 1 / min(ranks) if ranks else 0.0,
            "retrieved_relevant": len(hits), "relevant_files": len(relevant)}


def timing_summary(values):
    if not values:
        return {"samples": 0, "p50_ms": None, "p95_ms": None}
    ordered = sorted(values)
    return {"samples": len(values), "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3)}


def _mixed_pdf(path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (Available evidence 123.45) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_blank_page(width=300, height=300)
    with path.open("wb") as output:
        writer.write(output)


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def run_evaluation(mode="keyword", retrieval_config=None, k=5, repetitions=3, rerank=False):
    if mode not in {"keyword", "hybrid", "semantic"}:
        raise ValueError("Unsupported mode")
    if not 1 <= k <= 100 or not 1 <= repetitions <= 20:
        raise ValueError("Use k=1..100 and repetitions=1..20")
    if retrieval_config is not None and not isinstance(retrieval_config, dict):
        raise ValueError("retrieval_config must be an object")
    report = {
        "schema": "df-mcp-evaluation-v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "corpus": "generated_public_fixtures_v1", "requested_search_mode": mode,
        "requested_rerank": bool(rerank),
        "platform": platform.system(), "python": platform.python_version(), "k": k,
        "llm_answer_accuracy": {"evaluated": False, "reason": "No LLM answers generated or scored; these are retrieval and evidence-tool checks."},
        "scope": {"production_sources_accessed": False, "source_mutations": "Only generated temporary fixtures for version/delete checks",
                  "retrieval_models": "Explicit configuration may call embedding/reranking models with synthetic text only; defaults use no models."},
        "cases": [], "retrieval": {"cases": []}, "latency": {},
        "limitations": ["A small synthetic corpus does not establish production accuracy or throughput.",
                        "Correct returned evidence does not establish that an Agent will draw the correct conclusion.",
                        "No SMB, employee ACL inheritance, OCR accuracy, or visual table comprehension is evaluated.",
                        "p95 is descriptive for these few samples, not a service-level guarantee."],
    }
    timings = {name: [] for name in ("index", "search", "read", "export_and_calculate")}

    def measured(name, operation):
        start = time.perf_counter()
        try:
            return operation()
        finally:
            timings[name].append((time.perf_counter() - start) * 1000)

    def check(name, operation):
        start = time.perf_counter()
        try:
            detail = operation()
            result = {"id": name, "status": "passed", "observations": detail or {}}
        except Exception as exc:
            result = {"id": name, "status": "failed", "error_type": type(exc).__name__,
                      "reason": str(exc)[:500]}
        result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 3)
        report["cases"].append(result)

    with tempfile.TemporaryDirectory(prefix="df-mcp-evaluate-") as temporary:
        base = Path(temporary).resolve()
        source = base / "source"
        source.mkdir()
        for name, content in DOCUMENTS.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content.encode("utf-8"))
        _mixed_pdf(source / "mixed.pdf")
        initial_hashes = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in source.rglob("*") if p.is_file()}
        config = {"provider": "local", "root": str(source), "state_dir": str(base / "state"), "poll_seconds": 3600}
        if retrieval_config is not None:
            config["retrieval"] = retrieval_config
        config_path = base / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        engine = None
        try:
            engine = Engine(str(config_path), autostart=False)
            measured("index", engine.scan_once)
            report["engine_status"] = {key: value for key, value in engine.status().items()
                                       if key in {"search_mode", "coverage", "network_calls", "retrieval", "capabilities"}}
            inventory = []
            params = {"limit": 2}
            while True:
                page = engine.list(**params)
                inventory.extend(page["files"])
                if not page["has_more"]:
                    break
                params["cursor"] = page["next_cursor"]
            files = {item["path"]: item for item in inventory}

            def selection(name):
                item = files[name]
                verified = engine.inspect(item["file_id"])
                return {"file_id": item["file_id"], "expected_version": verified["source_version"]}

            def search(query, prefix="", selected_mode=mode):
                params = {"query": query, "prefix": prefix, "limit": k}
                if "mode" in inspect.signature(engine.search).parameters:
                    params["mode"] = selected_mode
                elif selected_mode != "keyword":
                    raise RuntimeError("This engine does not implement the requested search mode")
                if rerank and selected_mode == mode:
                    if "rerank" not in inspect.signature(engine.search).parameters:
                        raise RuntimeError("This engine does not implement requested reranking")
                    params["rerank"] = True
                result = measured("search", lambda: engine.search(**params))
                _require(result.get("ok"), "Search failed")
                return result

            def inventory_check():
                _require(set(files) == set(initial_hashes), "Enumeration omitted or added files")
                coverage = engine.status()["coverage"]
                _require(coverage["inventory_complete"] and coverage["all_present_files_indexed"], "Generated files were not completely indexed")
                _require(coverage["business_completeness"] == "not_established", "Inventory was incorrectly promoted to business completeness")
                return {"enumerated_files": len(files), "coverage": coverage}
            check("complete_paginated_inventory", inventory_check)

            for gold in RETRIEVAL_CASES:
                def retrieval_case(gold=gold):
                    result = search(gold["query"], gold.get("prefix", ""))
                    for _ in range(repetitions - 1):
                        search(gold["query"], gold.get("prefix", ""))
                    paths = [item["path"] for item in result["files"]]
                    metric = ranking_metrics(paths, gold["relevant_paths"], k)
                    entry = {**gold, "returned_paths": paths, **metric, "reported_mode": result.get("mode"),
                             "retrieval_diagnostics": result.get("retrieval", {})}
                    report["retrieval"]["cases"].append(entry)
                    diagnostics = result.get("retrieval", {})
                    if mode != "keyword":
                        semantic = diagnostics.get("semantic", {})
                        _require(semantic.get("query", {}).get("state") == "ready" and semantic.get("coverage_complete") is True,
                                 "Requested semantic component was unavailable or incomplete; lexical fallback is not a semantic-mode pass")
                    if rerank:
                        _require(diagnostics.get("rerank", {}).get("state") == "ready",
                                 "Requested reranking was unavailable; fallback is not a rerank pass")
                    _require(all(item["match"]["snippet_is_evidence"] is False for item in result["files"]), "Search snippets were promoted to evidence")
                    _require(result.get("exhaustive_for_business_totals") is False, "Search claimed business exhaustiveness")
                    if gold.get("prefix"):
                        _require(all(p.startswith(gold["prefix"] + "/") for p in paths), "Search escaped the requested prefix")
                    _require(metric["recall_at_k"] == 1.0, "Some gold files were not retrieved within K")
                    return entry
                check("retrieval_" + gold["id"], retrieval_case)

            def located_read():
                chosen = selection("policies/expense-approved.md")
                result = measured("read", lambda: engine.read(**chosen, limit=100))
                text = "".join(segment["text"] for segment in result["segments"])
                _require("CNY 120" in text and "alcohol is excluded" in text, "Amount or exception was lost")
                _require("CNY 180" not in text, "Draft content contaminated approved document")
                _require(result["source_version"] == initial_hashes["policies/expense-approved.md"], "Read version does not match source bytes")
                _require(all(segment["locator"] for segment in result["segments"]), "A source locator is missing")
                return {"path": result["path"], "source_version": result["source_version"],
                        "locators": [s["locator"] for s in result["segments"]],
                        "authority_note": "Fixture scope explicitly selects approved file; no inference from mtime."}
            check("versioned_read_preserves_amount_and_exception", located_read)

            def pagination_read():
                chosen = selection("notes/long.md")
                params = dict(chosen, limit=3)
                segments, pages = [], 0
                while True:
                    result = measured("read", lambda: engine.read(**params))
                    segments.extend(result["segments"])
                    pages += 1
                    _require(result["source_version"] == chosen["expected_version"], "Version changed during pagination")
                    if not result["has_more"]:
                        break
                    params["cursor"] = result["next_cursor"]
                _require("".join(s["text"] for s in segments) == DOCUMENTS["notes/long.md"], "Paginated read lost or duplicated source text")
                _require(len({s["ordinal"] for s in segments}) == len(segments), "Duplicate ordinals")
                return {"pages": pages, "segments": len(segments), "source_version": chosen["expected_version"]}
            check("read_pagination_is_lossless", pagination_read)

            def partial_pdf():
                result = measured("read", lambda: engine.read(**selection("mixed.pdf")))
                _require(result["extraction_complete"] is False, "Partial PDF claimed complete extraction")
                _require(result["content_coverage"]["missing_text_pages"] == [2], "Unreadable page gap was lost")
                _require(any(s["locator"].get("page") == 1 and "123.45" in s["text"] for s in result["segments"]), "Readable page lost text or locator")
                return {"extraction_complete": result["extraction_complete"], "content_coverage": result["content_coverage"]}
            check("partial_pdf_keeps_explicit_page_gap", partial_pdf)

            def no_match():
                result = search("ZXQAbsentTerm4917", selected_mode="keyword")
                _require(result["files"] == [], "Exact absent keyword unexpectedly matched")
                _require(result.get("exhaustive_for_business_totals") is False, "Zero search matches became an absence proof")
                return {"checked_mode": "keyword", "matches": 0,
                        "interpretation": "No lexical match in this fixture; no claim that a business fact does not exist."}
            check("zero_match_does_not_claim_business_absence", no_match)

            def aggregate():
                selected = [selection(name) for name in ("finance/april.csv", "finance/may.csv")]
                bundle = engine.evidence_bundle(selected, "All generated April and May rows; approved CNY only")
                _require(bundle["all_selected_versions_exported"] and bundle["all_selected_text_complete"], "Incomplete bundle")
                manifest = json.loads(Path(bundle["artifact_path"]).read_text(encoding="utf-8"))
                total, accepted = Decimal("0"), 0
                excluded = {"other_currency": 0, "draft": 0}
                for entry in manifest["files"]:
                    header = None
                    for raw in Path(entry["artifact_path"]).read_text(encoding="utf-8").splitlines():
                        row = json.loads(raw)
                        _require(row["source_version"] == entry["source_version"], "Export changed version")
                        values = [cell["value"] for cell in row["segment"]["data"]["cells"]]
                        if header is None:
                            header = values
                            continue
                        record = dict(zip(header, values))
                        if record["currency"] != "CNY":
                            excluded["other_currency"] += 1
                        elif record["status"] != "approved":
                            excluded["draft"] += 1
                        else:
                            total += Decimal(record["amount"])
                            accepted += 1
                _require(total == Decimal("400.30") and accepted == 4, "Deterministic total disagrees with gold")
                _require(excluded == {"other_currency": 1, "draft": 1}, "Exclusion population disagrees with gold")
                return {"currency": "CNY", "total": str(total), "accepted_rows": accepted,
                        "excluded_rows": excluded, "selected_files": 2, "arithmetic": "decimal",
                        "business_completeness": bundle["business_completeness"]}
            check("enumerated_versioned_exports_decimal_total", lambda: measured("export_and_calculate", aggregate))

            def unchanged_sources():
                after = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in source.rglob("*") if p.is_file()}
                _require(after == initial_hashes, "Read/index/export modified source bytes")
                return {"files": len(after), "source_bytes_unchanged": True}
            check("source_bytes_remain_read_only", unchanged_sources)

            def stale_version():
                chosen = selection("notes/changing.md")
                (source / "notes/changing.md").write_bytes(b"Revision two: changed evidence.\n")
                response = engine.call("read", chosen)
                _require(response.get("ok") is False and response["error"]["code"] in {"STALE_SOURCE", "SOURCE_CHANGED"}, "Changed source was served under old version")
                measured("index", engine.scan_once)
                response = engine.call("read", chosen)
                _require(response.get("ok") is False and response["error"]["code"] == "VERSION_MISMATCH", "Old version accepted after refresh")
                current = selection("notes/changing.md")
                _require(current["expected_version"] != chosen["expected_version"], "Source version did not change")
                return {"before_refresh": "stale_source_rejected", "after_refresh": "old_version_rejected"}
            check("source_change_invalidates_old_evidence", stale_version)

            def deletion():
                chosen = selection("notes/changing.md")
                (source / "notes/changing.md").unlink()
                measured("index", engine.scan_once)
                response = engine.call("read", chosen)
                _require(response.get("ok") is False and response["error"]["code"] == "FILE_NOT_FOUND", "Deleted document was still readable")
                _require(not engine.list(prefix="notes/changing.md")["files"], "Deleted document remained in inventory")
                return {"deleted_document_read": "FILE_NOT_FOUND"}
            check("deleted_source_is_removed", deletion)
        except Exception as exc:
            report["cases"].append({"id": "evaluation_setup", "status": "failed", "error_type": type(exc).__name__, "reason": str(exc)[:500]})
        finally:
            if engine is not None:
                engine.close()

    retrieval = report["retrieval"]["cases"]
    report["retrieval"].update({"expected_queries": len(RETRIEVAL_CASES), "measured_queries": len(retrieval),
                                "effective_modes": sorted({r["retrieval_diagnostics"].get("mode", r.get("reported_mode") or "unknown") for r in retrieval}),
                                "mean_recall_at_k": statistics.mean(r["recall_at_k"] for r in retrieval) if retrieval else None,
                                "mrr_at_k": statistics.mean(r["reciprocal_rank"] for r in retrieval) if retrieval else None})
    report["latency"] = {name: timing_summary(values) for name, values in timings.items()}
    passed = sum(case["status"] == "passed" for case in report["cases"])
    report["summary"] = {"passed": passed, "failed": len(report["cases"]) - passed, "total": len(report["cases"]),
                         "all_passed": passed == len(report["cases"]) and len(retrieval) == len(RETRIEVAL_CASES)}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Local JSON report path; no source files or configs are modified")
    parser.add_argument("--mode", choices=("keyword", "hybrid", "semantic"), default="keyword")
    parser.add_argument("--retrieval-config", type=Path, help="JSON retrieval settings object; applies only to generated fixtures")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--rerank", action="store_true", help="Require configured reranking to execute; a fallback fails this check")
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.retrieval_config.read_text(encoding="utf-8")) if args.retrieval_config else None
        report = run_evaluation(args.mode, config, args.k, args.repetitions, args.rerank)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output), **report["summary"], "llm_answer_accuracy_evaluated": False}, ensure_ascii=False))
    return 0 if report["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
