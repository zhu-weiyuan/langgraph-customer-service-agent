"""Validate and repair the v2 golden set against the current knowledge chunking.

This is intentionally deterministic: child ids are derived from the exact
chunk_document implementation used by scripts/ingest_knowledge.py.  It does
not connect to PostgreSQL and therefore cannot accidentally mutate the index.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.hybrid_rag import chunk_document  # noqa: E402
from agent.knowledge_sections import derive_golden_sections, load_all_sections  # noqa: E402
from agent.pgvector_hybrid import _section_for_chunk  # noqa: E402


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{lineno}: each row must be an object")
        row["_line"] = lineno
        rows.append(row)
    return rows


def build_index(kb_dir: Path) -> tuple[dict[str, list[dict]], dict[str, set[str]], dict[str, dict[str, list[str]]]]:
    sections = load_all_sections(kb_dir)
    chunk_ids: dict[str, set[str]] = {}
    section_chunks: dict[str, dict[str, list[str]]] = {}
    for md in sorted(kb_dir.glob("*.md")):
        source = md.stem
        text = md.read_text(encoding="utf-8")
        chunked = chunk_document(text, doc_id=source)
        chunk_ids[source] = {c["child_id"] for c in chunked["children"]}
        # A 300-character child can contain a heading and part of the next
        # section.  Ground-truth chunk membership is therefore based on span
        # overlap, not just the child start offset.
        source_sections = sections.get(source, [])
        by_section: dict[str, list[str]] = {}
        for index, section in enumerate(source_sections):
            start = int(section.get("start", 0))
            end = (int(source_sections[index + 1].get("start", len(text)))
                   if index + 1 < len(source_sections) else len(text))
            title = str(section.get("title", ""))
            for child in chunked["children"]:
                child_start = int(child.get("start", 0))
                child_end = child_start + len(child.get("text", ""))
                if max(start, child_start) < min(end, child_end):
                    by_section.setdefault(title, []).append(child["child_id"])
        section_chunks[source] = {
            title: list(dict.fromkeys(ids)) for title, ids in by_section.items()
        }
    return sections, chunk_ids, section_chunks


def source_ids(row: dict[str, Any]) -> list[str]:
    return list(dict.fromkeys(row.get("golden_context_ids") or row.get("golden_context") or []))


def repair_rows(rows: list[dict[str, Any]], kb_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    sections, chunk_ids, section_chunks = build_index(kb_dir)
    notes: list[str] = []
    for row in rows:
        row.pop("_line", None)
        ident = row.get("id", "<missing-id>")
        sources = source_ids(row)
        if row.get("should_refuse"):
            # Refusal cases are not retrieval recall cases.  Keeping stale
            # section/chunk goldens here makes reports look contradictory.
            if row.get("golden_sections") or row.get("golden_chunk_ids"):
                row["golden_sections"] = []
                row["golden_chunk_ids"] = []
                notes.append(f"{ident}: cleared retrieval goldens from refusal case")
            continue

        if not row.get("golden_sections"):
            derived = derive_golden_sections(row, kb_dir)
            if derived:
                row["golden_sections"] = derived
                notes.append(f"{ident}: derived golden_sections")

        golden_sections = row.get("golden_sections") or []
        valid_chunk_ids: list[str] = []
        for value in golden_sections:
            text = str(value)
            if "::" in text:
                source, title = text.split("::", 1)
                valid_chunk_ids.extend(section_chunks.get(source, {}).get(title, []))
            else:
                for source in sources:
                    valid_chunk_ids.extend(section_chunks.get(source, {}).get(text, []))
        valid_chunk_ids = list(dict.fromkeys(valid_chunk_ids))
        old = row.get("golden_chunk_ids") or []
        if old != valid_chunk_ids:
            row["golden_chunk_ids"] = valid_chunk_ids
            notes.append(f"{ident}: regenerated golden_chunk_ids ({len(old)} -> {len(valid_chunk_ids)})")
    return rows, notes


def validate(rows: list[dict[str, Any]], kb_dir: Path) -> tuple[list[str], list[str]]:
    sections, chunk_ids, section_chunks = build_index(kb_dir)
    errors: list[str] = []
    warnings: list[str] = []
    ids = Counter(row.get("id") for row in rows)
    for ident, count in ids.items():
        if not ident:
            errors.append("missing id")
        elif count > 1:
            errors.append(f"duplicate id: {ident} ({count})")
    for row in rows:
        ident = row.get("id", "<missing-id>")
        if row.get("layer") not in {"retrieval", "generation", "agent", "engineering"}:
            errors.append(f"{ident}: invalid layer {row.get('layer')!r}")
        if not row.get("query"):
            errors.append(f"{ident}: missing query")
        sources = source_ids(row)
        unknown_sources = [source for source in sources if source not in sections]
        if unknown_sources:
            errors.append(f"{ident}: unknown knowledge source(s): {unknown_sources}")
        metadata_source = (row.get("metadata_filter") or {}).get("source")
        if metadata_source and metadata_source not in sources:
            errors.append(f"{ident}: metadata_filter.source={metadata_source!r} not in golden sources {sources}")
        available_sections = {
            f"{source}::{section.get('title', '')}"
            for source in sources for section in sections.get(source, [])
        }
        bad_sections = [str(section) for section in row.get("golden_sections", [])
                        if "::" in str(section) and str(section) not in available_sections]
        if bad_sections:
            errors.append(f"{ident}: unknown golden section(s): {bad_sections}")
        known_chunks = {cid for source in sources for cid in chunk_ids.get(source, set())}
        bad_chunks = [str(cid) for cid in row.get("golden_chunk_ids", []) if str(cid) not in known_chunks]
        if bad_chunks:
            errors.append(f"{ident}: unknown golden chunk id(s): {bad_chunks}")
        if row.get("should_refuse"):
            if row.get("golden_sections") or row.get("golden_chunk_ids"):
                errors.append(f"{ident}: refusal case must not have retrieval goldens")
        else:
            if not sources:
                errors.append(f"{ident}: normal case has no golden source")
            if not row.get("golden_answer"):
                errors.append(f"{ident}: normal case has no golden_answer")
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(ROOT / "eval" / "golden_set_v2.jsonl"))
    parser.add_argument("--kb-dir", default=str(ROOT / "knowledge"))
    parser.add_argument("--repair", action="store_true", help="rewrite deterministic golden sections/chunk ids")
    args = parser.parse_args()
    dataset = Path(args.dataset)
    kb_dir = Path(args.kb_dir)
    rows = load_jsonl(dataset)
    notes: list[str] = []
    if args.repair:
        backup = dataset.with_suffix(dataset.suffix + ".pre_chunk_repair.bak")
        if not backup.exists():
            backup.write_text(dataset.read_text(encoding="utf-8"), encoding="utf-8")
        rows, notes = repair_rows(rows, kb_dir)
        dataset.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    errors, warnings = validate(rows, kb_dir)
    print(f"dataset={dataset} items={len(rows)} knowledge_files={len(list(kb_dir.glob('*.md')))}")
    print(f"repair_changes={len(notes)} errors={len(errors)} warnings={len(warnings)}")
    for note in notes:
        print(f"REPAIRED {note}")
    for warning in warnings:
        print(f"WARNING {warning}")
    for error in errors:
        print(f"ERROR {error}")
    if errors:
        return 1
    print("golden_set_validation_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
