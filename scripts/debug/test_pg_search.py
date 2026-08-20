from dotenv import load_dotenv
load_dotenv()
from agent.vector_rag import _get_embedding
from agent.pgvector_store import search

vec = _get_embedding('快递几天到')
print(f'Embedding dim: {len(vec)}')
results = search(vec, top_k=5)
print(f'Search results: {len(results)}')
for r in results[:3]:
    print(f'  {r["title"][:50]} score={r["score"]:.4f}')