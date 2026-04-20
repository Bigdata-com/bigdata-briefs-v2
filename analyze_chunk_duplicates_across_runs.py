#!/usr/bin/env python3
"""
Analyze chunk text duplicates across different runs (dates) for a given entity.

This script:
1. Finds all debug run folders for a given entity
2. Extracts all chunks from each run (from query_*.json files)
3. Compares chunks across runs to find text duplicates between dates
4. Reports chunks that appear in later runs with the same text as earlier runs
"""

import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime
import argparse


def find_entity_runs(debug_logs_dir: Path, entity_name: str) -> list[dict]:
    """
    Find all run folders for a given entity.
    
    Returns list of {folder, date, hash} sorted by date.
    """
    runs = []
    entity_prefix = entity_name.replace(" ", "_")
    
    for folder in debug_logs_dir.iterdir():
        if folder.is_dir() and folder.name.startswith(entity_prefix):
            # Parse folder name: EntityName_YYYY-MM-DD_hash
            parts = folder.name.rsplit("_", 2)
            if len(parts) >= 3:
                date_str = parts[-2]
                hash_str = parts[-1]
                try:
                    date = datetime.strptime(date_str, "%Y-%m-%d")
                    runs.append({
                        "folder": folder,
                        "date": date,
                        "date_str": date_str,
                        "hash": hash_str,
                    })
                except ValueError:
                    continue
    
    # Sort by date
    runs.sort(key=lambda x: x["date"])
    return runs


def extract_chunks_from_run(run_folder: Path) -> list[dict]:
    """
    Extract all chunks from a run's concept query files.
    
    - Excludes initial_check and exploratory queries (only concept queries)
    - Deduplicates by doc_id + chunk_num (like the main workflow)
    
    Returns list of {text, doc_id, chunk_num, headline, source, query_file}
    """
    chunks = []
    details_dir = run_folder / "details"
    
    if not details_dir.exists():
        return chunks
    
    for query_file in details_dir.glob("query_*.json"):
        # Skip initial_check and exploratory - only use concept queries
        filename = query_file.name.lower()
        if "initial_check" in filename or "exploratory" in filename:
            continue
        
        try:
            with open(query_file, "r") as f:
                data = json.load(f)
            
            results = data.get("response", {}).get("results", [])
            for result in results:
                doc_id = result.get("id", "")
                headline = result.get("headline", "")
                source = result.get("source", {}).get("name", "")
                
                for chunk in result.get("chunks", []):
                    chunks.append({
                        "text": chunk.get("text", "").strip(),
                        "doc_id": doc_id,
                        "chunk_num": chunk.get("cnum", 0),
                        "headline": headline,
                        "source": source,
                        "query_file": query_file.name,
                    })
        except Exception as e:
            print(f"  Warning: Could not read {query_file.name}: {e}")
    
    # Deduplicate by doc_id + chunk_num (keep first occurrence)
    seen_keys: set[str] = set()
    deduplicated: list[dict] = []
    
    for chunk in chunks:
        key = f"{chunk['doc_id']}-{chunk['chunk_num']}"
        if key not in seen_keys:
            seen_keys.add(key)
            deduplicated.append(chunk)
    
    raw_count = len(chunks)
    dedup_count = len(deduplicated)
    if raw_count != dedup_count:
        print(f"    Raw: {raw_count} → Dedup: {dedup_count} chunks")
    else:
        print(f"    {dedup_count} chunks (no duplicates)")
    
    return deduplicated


def find_cross_date_duplicates(runs: list[dict]) -> tuple[dict, list[dict]]:
    """
    Find chunks that appear in multiple runs with the same text.
    Also calculates per-run statistics (new vs repeated chunks).
    
    Returns:
        - duplicates: structure with duplicates grouped by text
        - run_stats: per-run statistics with new/repeated chunk counts
    """
    # Build a map: text -> list of {date, doc_id, chunk_num, headline, source}
    text_to_occurrences: dict[str, list[dict]] = defaultdict(list)
    
    # Track texts seen so far (cumulative across runs)
    seen_texts: set[str] = set()
    run_stats: list[dict] = []
    
    for run in runs:
        print(f"  Processing {run['date_str']} ({run['hash'][:8]})...")
        chunks = extract_chunks_from_run(run["folder"])
        
        # Count new vs repeated for this run
        total_chunks = 0
        new_chunks = 0
        repeated_chunks = 0
        
        for chunk in chunks:
            text = chunk["text"]
            if text:  # Skip empty texts
                total_chunks += 1
                
                if text in seen_texts:
                    repeated_chunks += 1
                else:
                    new_chunks += 1
                    seen_texts.add(text)
                
                text_to_occurrences[text].append({
                    "date": run["date_str"],
                    "doc_id": chunk["doc_id"],
                    "chunk_num": chunk["chunk_num"],
                    "headline": chunk["headline"],
                    "source": chunk["source"],
                })
        
        # Calculate percentages
        new_pct = round(new_chunks / total_chunks * 100, 1) if total_chunks > 0 else 0
        repeated_pct = round(repeated_chunks / total_chunks * 100, 1) if total_chunks > 0 else 0
        
        run_stats.append({
            "date": run["date_str"],
            "hash": run["hash"],
            "total_chunks": total_chunks,
            "new_chunks": new_chunks,
            "new_chunks_pct": new_pct,
            "repeated_chunks": repeated_chunks,
            "repeated_chunks_pct": repeated_pct,
        })
        
        # Log summary for this run
        print(f"    Total: {total_chunks} | New: {new_chunks} ({new_pct}%) | Repeated: {repeated_chunks} ({repeated_pct}%)")
    
    # Find texts that appear on multiple different dates
    duplicates = {}
    for text, occurrences in text_to_occurrences.items():
        unique_dates = set(occ["date"] for occ in occurrences)
        if len(unique_dates) > 1:
            duplicates[text] = {
                "text_preview": text[:200] + "..." if len(text) > 200 else text,
                "dates_count": len(unique_dates),
                "total_occurrences": len(occurrences),
                "dates": sorted(unique_dates),
                "occurrences": occurrences,
            }
    
    return duplicates, run_stats


def main():
    parser = argparse.ArgumentParser(
        description="Analyze chunk text duplicates across different runs for an entity"
    )
    parser.add_argument(
        "entity_name",
        help="Entity name (e.g., 'Adobe Inc' or 'Applied Materials Inc')"
    )
    parser.add_argument(
        "--debug-logs-dir",
        default="debug_logs",
        help="Path to debug_logs directory (default: debug_logs)"
    )
    parser.add_argument(
        "--output",
        help="Output JSON file path (default: duplicates_{entity}.json)"
    )
    
    args = parser.parse_args()
    
    # Resolve paths
    script_dir = Path(__file__).parent
    debug_logs_dir = Path(args.debug_logs_dir)
    if not debug_logs_dir.is_absolute():
        debug_logs_dir = script_dir / debug_logs_dir
    
    entity_name = args.entity_name
    output_file = args.output or f"duplicates_{entity_name.replace(' ', '_')}.json"
    
    print(f"\n=== Analyzing chunk duplicates for: {entity_name} ===\n")
    print(f"Debug logs directory: {debug_logs_dir}")
    
    # Find all runs for this entity
    runs = find_entity_runs(debug_logs_dir, entity_name)
    
    if not runs:
        print(f"\nNo runs found for entity '{entity_name}'")
        return
    
    print(f"\nFound {len(runs)} runs:")
    for run in runs:
        print(f"  - {run['date_str']} ({run['hash'][:8]})")
    
    # Find duplicates across dates
    print(f"\nExtracting chunks from each run...")
    duplicates, run_stats = find_cross_date_duplicates(runs)
    
    # Summary
    print(f"\n=== Results ===")
    print(f"Total unique texts appearing across multiple dates: {len(duplicates)}")
    
    # Sort by number of dates (most repeated first)
    sorted_dups = sorted(
        duplicates.items(),
        key=lambda x: (x[1]["dates_count"], x[1]["total_occurrences"]),
        reverse=True
    ) if duplicates else []
    
    if sorted_dups:
        print(f"\nTop 10 most repeated across dates:")
        for i, (text, info) in enumerate(sorted_dups[:10]):
            print(f"\n  {i+1}. Appears on {info['dates_count']} dates, {info['total_occurrences']} total occurrences")
            print(f"     Dates: {', '.join(info['dates'])}")
            print(f"     Preview: {info['text_preview'][:100]}...")
    
    # Save full report (always, even if no duplicates - we want the run stats)
    output = {
        "entity": entity_name,
        "analyzed_runs": run_stats,  # Now includes full stats per run
        "total_cross_date_duplicates": len(duplicates),
        "duplicates": [
            {
                "text_preview": info["text_preview"],
                "dates_count": info["dates_count"],
                "total_occurrences": info["total_occurrences"],
                "dates": info["dates"],
                "occurrences": info["occurrences"],
            }
            for text, info in sorted_dups
        ],
    }
    
    output_path = script_dir / output_file
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"\nFull report saved to: {output_path}")


if __name__ == "__main__":
    main()

