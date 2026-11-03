"""Reproducible lexical-retrieval smoke evaluation, not a general RAG benchmark."""

import json
from pathlib import Path

from backend.ingest import build_document
from backend.store import VectorStore


def evaluate():
    fixture = json.loads(Path(__file__).with_name("fixtures.json").read_text())
    store = VectorStore()
    rows, hits, reciprocals, abstentions = [], [], [], []
    try:
        for doc in fixture["documents"]:
            data = doc["text"].encode()
            store.add_document(build_document(doc["name"], data, 0), len(data))
        for query in fixture["queries"]:
            found = [row["doc"] for row in store.search(query["question"], top_k=3)]
            expected = query["expected"]
            if expected:
                hits.append(bool(found and found[0] == expected))
                reciprocals.append(
                    1 / (found.index(expected) + 1) if expected in found else 0
                )
            else:
                abstentions.append(not found)
            rows.append(
                {
                    "question": query["question"],
                    "expected": expected,
                    "retrieved": found,
                }
            )
    finally:
        store.close()
    report = {
        "documents": len(fixture["documents"]),
        "answerable_queries": len(hits),
        "unanswerable_queries": len(abstentions),
        "hit_at_1": sum(hits) / len(hits),
        "mrr_at_3": sum(reciprocals) / len(reciprocals),
        "zero_overlap_abstention": sum(abstentions) / len(abstentions),
        "queries": rows,
    }
    return report


if __name__ == "__main__":
    report = evaluate()
    print(json.dumps(report, indent=2))
    if report["hit_at_1"] < 0.9 or report["zero_overlap_abstention"] != 1:
        raise SystemExit("Retrieval regression: inspect the per-query results above.")
