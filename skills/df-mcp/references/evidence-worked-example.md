# Simulated CSV evidence → a traceable subtotal

Read this example when turning versioned CSV exports into a calculation. All values below come from this repository's **entirely simulated** `examples/sample-drive`; no real source access or MCP call is needed to understand or test it. It illustrates a calculation pattern, not a universal budget definition or an authority registry.

## Establish the sample's meaning before calculating

The sample's `README.md` selects `budget-approved.csv` as its official source and excludes `budget-draft.csv`. Its `terms.md` defines the September 2026, CNY, tax-inclusive balance as:

`approved_budget − actual_spend − committed_only`

Here `committed_only` explicitly excludes amounts already in `actual_spend`. For other data, this formula could double-count commitments or use the wrong period; verify the actual metric and approval evidence first. In a real workflow, preserve the source versions and locators of that approval/definition evidence alongside the calculation ledger. An `approved` cell alone does not establish file authority.

After enumerating the agreed population and inspecting its files, create a bundle or individual exports. Load **both** the manifest and the returned JSONL artifacts. Each JSONL line wraps a segment as follows; the IDs and version below are visibly artificial placeholders, never values to send to MCP:

```json
{"file_id":"SIMULATED-approved","path":"budget-approved.csv","source_version":"SIMULATED-version","ordinal":1,"segment":{"locator":{"kind":"row","sheet":"data","row":2},"text":"…","data":{"cells":[{"coordinate":"A2","value":"P-001","value_type":"string"},{"coordinate":"B2","value":"星河计划","value_type":"string"}]}}}
```

This abbreviated schema illustration omits the remaining cells. Use the actual full `segment.data.cells` values, not `text` or a search snippet. CSV record 1 is the header **in this sample**. Record numbers are not physical line numbers when quoted values contain newlines. Export `row_count` and bundle `segment_count` count segments, including the header.

## A small, deliberately scoped Python example

`texts` contains the loaded JSONL strings. `selected` is keyed by actual file IDs and contains each successful export's `path`, `source_version` and `segment_count` from the inspected evidence/manifest. `official_ids` is the explicit file selection justified by the approval evidence above; do not derive it from filenames. Check manifest failures, extraction gaps and warnings before calling this function.

The example accepts the sample's small CSV records: one complete segment per record, a header at row 1, and amounts with at most two fractional digits. It stops on split rows/cells, mixed versions, missing segments or malformed headers instead of silently treating fragments as rows. Adapt reconstruction before using it for other CSV layouts. Business duplicates are kept as unresolved gaps; it does not choose a winner automatically.

```python
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation


def calculate(texts, selected, official_ids, period, currency):
    rows = defaultdict(dict)
    counts = Counter()
    for text in texts:
        for line in text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            fid = item["file_id"]
            expected = selected[fid]
            if any(item[k] != expected[k] for k in ("path", "source_version")):
                raise ValueError("Unexpected file identity or mixed source version")
            segment = item["segment"]
            loc, cells = segment["locator"], segment["data"]["cells"]
            row = loc["row"]
            if (loc.get("kind") != "row" or row in rows[fid]
                    or any(c.get("value_parts", 1) != 1 for c in cells)):
                raise ValueError("This example requires complete, unsplit CSV records")
            rows[fid][row] = [c["value"] for c in cells]
            counts[fid] += 1
    if (set(rows) != set(selected) or not official_ids
            or not set(official_ids) <= set(selected)):
        raise ValueError("Selected exports are missing or official selection is invalid")

    ledger, candidates = [], []
    scope_fields = ("project_id", "period", "currency", "status")
    money_fields = ("approved_budget", "actual_spend", "committed_only")
    for fid, records in rows.items():
        if counts[fid] != selected[fid]["segment_count"]:
            raise ValueError("Export segment count does not reconcile")
        header = records[1]
        if any(not h for h in header) or len(header) != len(set(header)):
            raise ValueError("Missing or duplicated CSV column name")
        for row, values in sorted(records.items()):
            if row == 1:
                continue
            source = dict(selected[fid], file_id=fid, row=row)
            entry = {"source": source, "raw_values": values, "reasons": []}
            ledger.append(entry)
            if len(values) != len(header):
                entry.update(decision="gap", reasons=["row_width_mismatch"])
                continue
            data = dict(zip(header, values))
            entry["values"] = data
            if fid not in official_ids:
                entry.update(decision="excluded", reasons=["source_not_selected_as_official"])
                continue
            if any(not data.get(k, "").strip() for k in scope_fields):
                entry.update(decision="gap", reasons=["missing_scope_or_identity_field"])
                continue
            filters = {"period": period, "currency": currency, "status": "approved"}
            entry["reasons"] = ["outside_" + k for k, v in filters.items() if data[k] != v]
            if entry["reasons"]:
                entry["decision"] = "excluded"
                continue
            entry["key"] = tuple(data[k] for k in ("project_id", "period", "currency"))
            candidates.append(entry)
            amounts = []
            for field in money_fields:
                try:
                    value = Decimal(data.get(field, "").strip())
                    if not value.is_finite() or value != value.quantize(Decimal("0.01")):
                        raise InvalidOperation
                    amounts.append(value)
                except (InvalidOperation, ValueError):
                    entry["reasons"].append("missing_or_invalid_" + field)
            if not entry["reasons"]:
                entry["balance"] = amounts[0] - amounts[1] - amounts[2]

    duplicates = Counter(e["key"] for e in candidates)
    subtotal = Decimal("0.00")
    for entry in candidates:
        if duplicates[entry["key"]] > 1:
            entry["reasons"].append("unresolved_duplicate_business_key")
        entry["decision"] = "gap" if entry["reasons"] else "included"
        if entry["decision"] == "included":
            subtotal += entry["balance"]
        if "balance" in entry:
            entry["balance"] = format(entry["balance"], ".2f")
    included_count = sum(e["decision"] == "included" for e in ledger)
    return {
        "period": period, "currency": currency,
        "included_row_count": included_count,
        "selected_row_subtotal": format(subtotal, ".2f") if included_count else None,
        "has_unresolved_rows": any(e["decision"] == "gap" for e in ledger),
        "business_completeness": "not_established", "ledger": ledger,
    }
```

Pass `period="2026-09"`, `currency="CNY"`, the two sample budget exports, and only the approved file's ID in `official_ids`. Preserve the returned ledger; each included row's three inputs and balance share the exact `source.path`, `source.file_id`, `source.source_version` and `source.row`. Cite those runtime values, not the artificial schema placeholders above. The draft row is retained with `source_not_selected_as_official`.

| Simulated project | CSV record | Approved budget | Actual spend | Committed only | Balance, CNY |
|---|---:|---:|---:|---:|---:|
| P-001 | 2 | 100,000.00 | 40,000.25 | 12,000.00 | 47,999.75 |
| P-002 | 3 | 50,000.00 | 21,000.10 | 5,000.00 | 23,999.90 |
| P-003 | 4 | 30,000.00 | 32,000.00 | 0.00 | −2,000.00 |
| Selected subtotal | | 180,000.00 | 93,000.35 | 17,000.00 | **69,999.65** |

Useful synthetic checks: a blank P-002 spend leaves it as a gap and yields a **45,999.75 bounded subtotal**; a second P-001 record excludes **both** ambiguous candidates and yields **21,999.90**. A row in another month/currency or with another status remains in the ledger with its exclusion reasons. With no supported rows, the subtotal is `None`, not zero. Never convert gaps to zero or describe either bounded subtotal as the complete budget balance.

Even the clean sample calculation returns `business_completeness: not_established`. Reconcile the agreed population, authority, exclusions and required definitions separately before calling the result a complete formal total. A bundle only preserves the selected evidence and its observation interval; it cannot close that business scope itself.
