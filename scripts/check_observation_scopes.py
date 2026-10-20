#!/usr/bin/env python3
"""Detect, and optionally repair, Hindsight observations that merge several sources.

`recall` can only cite an observation that traces back to exactly one local source
version. Observations are consolidated per tag set, and every retain carries the tag
`src:<source_id>:v<version>`. Facts retained before that tag existed share the untagged
scope, where Hindsight may have merged facts of different sources into one observation.

  check_observation_scopes.py            report only; exit 1 when a merged observation exists
  check_observation_scopes.py --apply    reset the facts behind merged observations so
                                         Hindsight re-consolidates them under their own tags

Run `curate reindex SOURCE_ID` for the affected journal sources first so their facts carry
the per-source tag; resetting an untagged fact would only recreate the same merge.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

# The daemon requires LifeContext's engine token; set from --token-file in main().
HEADERS: dict[str, str] = {}


def call(base: str, path: str, method: str = "GET") -> dict:
    request = urllib.request.Request(base + path, method=method, headers=HEADERS)
    # Never through a proxy: the request carries the token.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=120) as response:
        body = response.read()
    return json.loads(body) if body else {}


def list_memories(base: str) -> list[dict]:
    items: list[dict] = []
    offset = 0
    while True:
        page = call(base, f"/memories/list?limit=500&offset={offset}")
        items += page["items"]
        offset += 500
        if offset >= page["total"] or not page["items"]:
            return items


def sources_of(base: str, memory_id: str, cache: dict, seen: tuple = ()) -> set[tuple[str, str]]:
    if memory_id not in cache:
        cache[memory_id] = call(base, f"/memories/{memory_id}")
    memory = cache[memory_id]
    found: set[tuple[str, str]] = set()
    document = memory.get("document_id")
    if document:
        source = document.split(":")[1] if document.startswith("doc:") else document
        found.add((source, str((memory.get("metadata") or {}).get("source_version", ""))))
    for parent in memory.get("source_memory_ids") or []:
        if parent not in seen:
            found |= sources_of(base, parent, cache, seen + (memory_id,))
    return found


def merged_observations(base: str, items: list[dict]) -> list[tuple[dict, set]]:
    cache: dict = {}
    merged = []
    for item in items:
        if item["fact_type"] == "observation":
            sources = sources_of(base, item["id"], cache)
            if len(sources) != 1:
                merged.append((item, sources))
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:19007")
    parser.add_argument("--bank", default="lifeweave")
    parser.add_argument("--apply", action="store_true", help="reset facts behind merged observations")
    parser.add_argument("--wait", type=int, default=900, help="seconds to wait for re-consolidation")
    parser.add_argument("--token-file", type=Path, default=Path.home() / ".lifeweave" / "hindsight-api.token",
                        help="the engine token LifeContext made (default: ~/.lifeweave/hindsight-api.token)")
    args = parser.parse_args()
    HEADERS["Authorization"] = "Bearer " + args.token_file.read_text(encoding="ascii").strip()
    base = f"{args.base_url}/v1/default/banks/{args.bank}"

    items = list_memories(base)
    merged = merged_observations(base, items)
    affected = {source for _, sources in merged for source, _ in sources}
    scopes = call(base, "/observations/scopes")
    print(f"observations={sum(i['fact_type'] == 'observation' for i in items)} "
          f"merged_or_unattributable={len(merged)} sources_involved={len(affected)}")
    print("scopes:", json.dumps(scopes)[:300])
    for _, sources in merged[:10]:
        print("  merged:", sorted(source for source, _ in sources))
    if not merged:
        return 0
    if not args.apply:
        return 1

    facts = [i for i in items if i["fact_type"] != "observation" and i.get("document_id") in affected]
    untagged = [i for i in facts if not i.get("tags")]
    if untagged:
        print(f"refusing: {len(untagged)} affected facts are untagged; run `curate reindex` for their sources first",
              file=sys.stderr)
        return 2
    for fact in facts:
        call(base, f"/memories/{fact['id']}/observations", method="DELETE")
    print(f"reset {len(facts)} facts for re-consolidation")
    deadline = time.time() + args.wait
    while time.time() < deadline:
        time.sleep(15)
        ids = {fact["id"] for fact in facts}
        pending = [i for i in list_memories(base) if i["id"] in ids and not i.get("consolidated_at")]
        print(f"  waiting: {len(pending)} facts not yet consolidated", flush=True)
        if not pending:
            break
    remaining = merged_observations(base, list_memories(base))
    print(f"after repair: merged_or_unattributable={len(remaining)}")
    return 0 if not remaining else 1


if __name__ == "__main__":
    raise SystemExit(main())
