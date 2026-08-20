#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check_pgvector_chunks — 核实 pgvector 里的父子分块 / 向量 / 重排模型是否真的可用。

为什么需要这个脚本:
  父子分块在检索链路里是**优雅降级**的 —— child 命中若没有 parent_id,
  map_children_to_parents 会原样透传(orphan),不报错、不打日志。
  也就是说:代码接对了,但库里没写 parent 行,这一环就是静默空转。
  Cross-Encoder 同理:sentence_transformers 缺失会降级成 RuleReranker。

  所以"代码里有"不等于"跑起来生效"。这个脚本直接查库 + 实际加载模型来验证。

用法:
    python scripts\\check_pgvector_chunks.py
    python scripts\\check_pgvector_chunks.py --query "音箱没声音"   # 顺带跑一次检索
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

OK, BAD, WARN = "[OK]  ", "[FAIL]", "[WARN]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="", help="附带跑一次真实检索,打印命中层级")
    args = ap.parse_args()

    fails = 0

    # ── 1) 连库 ──
    dsn = (os.environ.get("PG_DSN") or os.environ.get("RAG_PG_DSN")
           or os.environ.get("DATABASE_URL")
           or "postgresql://postgres:postgres@localhost:5432/agent")
    safe = dsn.split("@")[-1]
    print(f"DSN: ...@{safe}")
    try:
        import psycopg
        conn = psycopg.connect(dsn, autocommit=True)
    except Exception as exc:
        print(f"{BAD} 连不上 pgvector: {type(exc).__name__}: {exc}")
        return 1
    print(f"{OK} 已连接")

    cur = conn.cursor()

    # ── 2) 表存在吗 ──
    cur.execute("SELECT to_regclass('rag_chunks') IS NOT NULL")
    if not cur.fetchone()[0]:
        print(f"{BAD} 没有 rag_chunks 表 —— 先跑 scripts\\ingest_knowledge.py 灌库")
        return 1
    print(f"{OK} rag_chunks 表存在")

    # ── 3) 父子分块:parent 行 / child 行 / 挂载率 ──
    cur.execute("SELECT count(*) FROM rag_chunks")
    total = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM rag_chunks WHERE is_parent")
    parents = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM rag_chunks WHERE NOT is_parent")
    children = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM rag_chunks "
                "WHERE NOT is_parent AND parent_id IS NOT NULL")
    linked = cur.fetchone()[0]

    print(f"\n── 父子分块 ──")
    print(f"  总块数 {total}  parent {parents}  child {children}  "
          f"child 带 parent_id {linked}")
    if parents == 0:
        print(f"{BAD} 没有任何 parent 行 —— 父子分块**没有生效**,"
              f"检索会走 orphan 透传(不报错但等于没有这一环)")
        print(f"       修:python scripts\\ingest_knowledge.py --child-size 300 "
              f"--parent-size 1200")
        fails += 1
    elif children == 0:
        print(f"{BAD} 只有 parent 没有 child —— 灌库参数不对")
        fails += 1
    elif linked < children:
        rate = linked / children
        print(f"{WARN} 只有 {rate:.1%} 的 child 挂到了 parent,其余会 orphan 透传")
        if rate < 0.9:
            fails += 1
    else:
        print(f"{OK} 父子分块生效:child 全部挂载,命中后会映射回 parent 大块")

    # ── 4) 向量列 ──
    cur.execute("SELECT count(*) FROM rag_chunks WHERE embedding IS NOT NULL")
    with_vec = cur.fetchone()[0]
    print(f"\n── 向量 ──")
    print(f"  有 embedding 的块 {with_vec}/{total}")
    if with_vec == 0:
        print(f"{BAD} 一条向量都没有 —— 向量检索会整条腿空转,只剩 BM25")
        fails += 1
    else:
        cur.execute("SELECT vector_dims(embedding) FROM rag_chunks "
                    "WHERE embedding IS NOT NULL LIMIT 1")
        dims = cur.fetchone()[0]
        print(f"{OK} 维度 {dims}"
              + ("" if dims <= 2000 else "  ← 超过 2000,HNSW/IVFFlat 建不了索引!"))
        if dims > 2000:
            fails += 1

    # ── 5) 向量索引(决定是精确扫描还是近邻索引)──
    cur.execute("SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename='rag_chunks'")
    idx = cur.fetchall()
    vec_idx = [n for n, d in idx if "hnsw" in d.lower() or "ivfflat" in d.lower()]
    print(f"  索引 {len(idx)} 个,其中向量索引 {vec_idx or '无'}")
    if not vec_idx:
        print(f"{WARN} 没有 HNSW/IVFFlat,向量检索是全表精确扫描 —— "
              f"数据量小无妨,上量会慢")

    # ── 6) Cross-Encoder 是真模型还是降级了 ──
    print(f"\n── 重排 ──")
    try:
        from agent.hybrid_rag import CrossEncoderReranker
        rr = CrossEncoderReranker()
        name = rr.model_name
        if not rr.available:
            print(f"{BAD} sentence_transformers 不可用 —— CrossEncoderReranker "
                  f"已静默降级成 RuleReranker(纯规则,不是重排模型)")
            print(f"       修:pip install sentence-transformers  "
                  f"(首次 rerank 会下载 {name})")
            fails += 1
        else:
            # 模型是**懒加载**的,必须真跑一次 rerank 才知道能不能加载成功
            scored = rr.rerank("音箱没声音", [
                {"title": "a", "content": "音箱无声排查:检查电源与音量", "score": 0.1},
                {"title": "b", "content": "发票开具流程说明", "score": 0.9},
            ], top_n=2)
            if getattr(rr, "_model", None) is None:
                print(f"{BAD} 库在但模型加载失败({name})—— 实际走的是 RuleReranker")
                print(f"       多半是下载不通,可先手动 huggingface 拉模型,"
                      f"或用 RAG_RERANKER_MODEL 换本地路径")
                fails += 1
            else:
                top = (scored[0].get("title") if scored else "?")
                print(f"{OK} 重排模型 {name} 真实加载成功")
                print(f"     实测:输入分数 b(0.9) > a(0.1),重排后第一名 = '{top}'"
                      f"(期望 a,即重排推翻了原始排序)")
                if top != "a":
                    print(f"{WARN} 重排没能纠正排序,模型可能不对口")
    except Exception as exc:
        print(f"{BAD} 重排检查异常: {type(exc).__name__}: {exc}")
        fails += 1

    # ── 7) 端到端检索(可选)──
    if args.query:
        print(f"\n── 端到端检索 RAG_BACKEND=pgvector ──")
        os.environ["RAG_BACKEND"] = "pgvector"
        try:
            from agent.rag_backend import retrieve
            hits = retrieve(args.query, top_k=5)
            print(f"  命中 {len(hits)} 条:")
            for i, h in enumerate(hits, 1):
                cid = h.get("chunk_id") or h.get("id") or "?"
                lvl = "parent" if ":c" not in str(cid) else "child(未映射)"
                print(f"   {i}. [{lvl}] {str(h.get('title'))[:28]}  "
                      f"score={h.get('score')}")
            if hits and all(":c" in str(h.get("chunk_id") or "") for h in hits):
                print(f"{WARN} 返回的全是 child —— parent 映射没起作用")
        except Exception as exc:
            print(f"{BAD} 检索失败: {type(exc).__name__}: {exc}")
            fails += 1

    print("\n" + "=" * 60)
    print("全部通过 ✓" if fails == 0 else f"有 {fails} 项不通过,见上面 [FAIL]")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
