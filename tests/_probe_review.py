import pathlib
import tempfile

from water_agent.ingestion.pipeline import ingest_to_trivium
from water_agent.retrieval.trivium import TriviumRetriever

fixture = pathlib.Path("data/fixtures/rag_fixture.jsonl")
with tempfile.TemporaryDirectory() as tmp:
    db_path = pathlib.Path(tmp) / "p1.db"
    print("db_path", db_path, flush=True)
    s1 = ingest_to_trivium(fixture, db_path)
    print("s1", s1, flush=True)
    s2 = ingest_to_trivium(fixture, db_path)
    print("s2", s2, flush=True)
    parents_path = pathlib.Path(str(db_path) + ".parents.json")
    print("parents exists", parents_path.exists(), flush=True)
    retriever = TriviumRetriever(db_path=db_path)
    print("fallback", retriever.is_fallback, flush=True)
    hits = retriever.retrieve("污水管道最大充满度", top_k=5)
    print("hits", len(hits), [h.source_id for h in hits], flush=True)
    retriever.close()
