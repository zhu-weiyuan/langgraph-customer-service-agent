#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/_local_retriever.py — 容器内零依赖真实检索器(BM25 + 规则改写)

设计目标:本容器无 LLM / embedding / jieba,但 knowledge/*.md 是真实知识库。
本模块用纯 Python 实现可信检索,支撑"普通 RAG vs Agentic RAG"的真实对比:

    1) 语料加载 load_corpus()
       读 knowledge/*.md,按 `## ` 切节 → [{title, text, source}]
       source = 文件名去扩展名(如 returns-refunds)

    2) 分词 tokenize()
       中文:字符 bigram + 单字 混合(不依赖 jieba)
       英文/数字:小写单词整体 + 其字符本身
       => 既能命中"退货流程"这种词组,又能兜住错别字/口语的部分重叠

    3) BM25(k1=1.5, b=0.75)  纯 Python,可手算校验

    4) LocalRetriever.retrieve(query, top_k) -> [{title,text,source,score}]

    5) rewrite(query) 规则改写(模拟 Agentic 的 Query Rewrite,无 LLM):
       同义词表(agent/synonyms.json)+ 疑问词/语气词剥离 + 关键词提取
       返回 [原query, 变体1, 变体2, ...](去重,3~4 条)

纯标准库,`python -c "import scripts._local_retriever"` 与 py_compile 均可通过。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
_KNOWLEDGE_DIR = _ROOT / "knowledge"
_SYNONYMS_PATH = _ROOT / "agent" / "synonyms.json"

# ──────────────────────────────────────────────────────────────────
# 分词:中文字符 bigram + 单字;英文/数字整词 + 字符
# ──────────────────────────────────────────────────────────────────
_CJK = re.compile(r"[一-鿿]")
_TOKEN_SPLIT = re.compile(r"[a-zA-Z0-9]+|[一-鿿]|[^\sa-zA-Z0-9一-鿿]")
# 中文停用/疑问词(改写时剥离);检索分词保留以免误删语义
_STOPWORDS = {
    "的", "了", "吗", "呢", "啊", "呀", "吧", "哦", "嘛", "怎么", "怎样", "如何",
    "什么", "多久", "多少", "哪", "哪个", "哪里", "哪些", "是不是", "能不能",
    "可以", "请问", "一下", "这个", "那个", "我", "我的", "你", "它", "他",
    "有", "没", "要", "会", "想", "呀", "咋",
}


def tokenize(text: str) -> List[str]:
    """混合分词:CJK 用 bigram+单字,英数字整词+字符。零外部依赖。"""
    if not text:
        return []
    text = text.lower()
    atoms = _TOKEN_SPLIT.findall(text)
    tokens: List[str] = []
    cjk_run: List[str] = []

    def _flush_cjk() -> None:
        # 单字
        tokens.extend(cjk_run)
        # bigram(相邻两字)
        for i in range(len(cjk_run) - 1):
            tokens.append(cjk_run[i] + cjk_run[i + 1])
        cjk_run.clear()

    for a in atoms:
        if _CJK.match(a):
            cjk_run.append(a)
        else:
            _flush_cjk()
            if a.strip() and re.match(r"[a-zA-Z0-9]+", a):
                tokens.append(a)              # 整词(E001 / wifi / x100)
                if len(a) > 3:                # 长英数字再拆字符 bigram 兜错别字
                    for i in range(len(a) - 1):
                        tokens.append(a[i:i + 2])
    _flush_cjk()
    return tokens


# ──────────────────────────────────────────────────────────────────
# 语料加载:knowledge/*.md → 段
# ──────────────────────────────────────────────────────────────────
def load_corpus(knowledge_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    kdir = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
    corpus: List[Dict[str, Any]] = []
    for md in sorted(kdir.glob("*.md")):
        source = md.stem
        title = source
        buf: List[str] = []
        for line in md.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if buf:
                    corpus.append({"title": title, "text": "\n".join(buf).strip(),
                                   "source": source})
                    buf = []
                title = line[3:].strip()
            elif line.startswith("# "):
                continue                       # 文档一级标题跳过
            else:
                buf.append(line)
        if buf:
            corpus.append({"title": title, "text": "\n".join(buf).strip(),
                           "source": source})
    return corpus


# ──────────────────────────────────────────────────────────────────
# BM25(k1=1.5, b=0.75)
# ──────────────────────────────────────────────────────────────────
class BM25:
    def __init__(self, docs_tokens: Sequence[Sequence[str]],
                 k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs_tokens = [list(t) for t in docs_tokens]
        self.N = len(self.docs_tokens)
        self.doc_len = [len(t) for t in self.docs_tokens]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        self.tf: List[Counter] = [Counter(t) for t in self.docs_tokens]
        df: Counter = Counter()
        for c in self.tf:
            df.update(c.keys())
        # BM25 idf(带 +1 平滑,恒为正,避免高频词负分)
        self.idf: Dict[str, float] = {
            term: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for term, n in df.items()
        }

    def score(self, query_tokens: Sequence[str], idx: int) -> float:
        tf = self.tf[idx]
        dl = self.doc_len[idx]
        s = 0.0
        for term in query_tokens:
            if term not in tf:
                continue
            f = tf[term]
            idf = self.idf.get(term, 0.0)
            denom = f + self.k1 * (1 - self.b + self.b * (dl / self.avgdl if self.avgdl else 0))
            s += idf * (f * (self.k1 + 1)) / (denom if denom else 1.0)
        return s

    def search(self, query_tokens: Sequence[str], top_k: int) -> List[tuple]:
        scored = [(i, self.score(query_tokens, i)) for i in range(self.N)]
        scored = [(i, sc) for i, sc in scored if sc > 0.0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]


# ──────────────────────────────────────────────────────────────────
# 检索器
# ──────────────────────────────────────────────────────────────────
class LocalRetriever:
    def __init__(self, knowledge_dir: Optional[Path] = None) -> None:
        self.corpus = load_corpus(knowledge_dir)
        # 标题权重更高:标题重复 2 次拼进可检索文本
        self._doc_tokens = [
            tokenize((c["title"] + " ") * 3 + c["text"]) for c in self.corpus
        ]
        self.bm25 = BM25(self._doc_tokens)

    def retrieve(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        qt = tokenize(query)
        results: List[Dict[str, Any]] = []
        for idx, score in self.bm25.search(qt, top_k):
            c = self.corpus[idx]
            results.append({"title": c["title"], "text": c["text"],
                            "source": c["source"], "score": round(float(score), 4)})
        return results


# ──────────────────────────────────────────────────────────────────
# 规则改写(模拟 Agentic Query Rewrite,无 LLM)
# ──────────────────────────────────────────────────────────────────
def _load_synonyms() -> Dict[str, List[str]]:
    if _SYNONYMS_PATH.exists():
        try:
            return json.loads(_SYNONYMS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


_SYNONYMS = _load_synonyms()


def _strip_question_words(query: str) -> str:
    """剥离疑问词/语气词,留下核心关键词串。"""
    s = query
    for w in sorted(_STOPWORDS, key=len, reverse=True):
        s = s.replace(w, " ")
    s = re.sub(r"[?？!！。,,、\s]+", " ", s).strip()
    return s


def _keyword_extract(query: str) -> str:
    """抽取有信息量的词:英数字 token + 长度≥2 的中文段。"""
    atoms = re.findall(r"[a-zA-Z0-9]+|[一-鿿]+", query.lower())
    kept: List[str] = []
    for a in atoms:
        if re.match(r"[a-zA-Z0-9]+", a):
            kept.append(a)
        else:
            core = a
            for w in _STOPWORDS:
                core = core.replace(w, "")
            if len(core) >= 2:
                kept.append(core)
    return " ".join(kept)


def rewrite(query: str, max_variants: int = 3) -> List[str]:
    """规则改写:返回 [原query, 变体...](去重)。模拟 Agentic 多变体召回。

    变体来源:
      1) 同义词替换(agent/synonyms.json)
      2) 疑问词/语气词剥离后的关键词串
      3) 纯关键词抽取串
    """
    variants: List[str] = [query]

    # 1) 同义词扩展:命中的原词 → 追加"原query + 同义词"变体
    syn_terms: List[str] = []
    for k, vs in _SYNONYMS.items():
        if k in query:
            syn_terms.extend(vs)
    if syn_terms:
        variants.append(query + " " + " ".join(dict.fromkeys(syn_terms)))

    # 2) 疑问词剥离
    stripped = _strip_question_words(query)
    if stripped and stripped != query:
        variants.append(stripped)

    # 3) 关键词抽取
    kw = _keyword_extract(query)
    if kw and kw not in variants:
        variants.append(kw)

    # 去重、保序、限量
    out: List[str] = []
    for v in variants:
        v = v.strip()
        if v and v not in out:
            out.append(v)
    return out[: max_variants + 1]


# 自测
if __name__ == "__main__":
    r = LocalRetriever()
    print(f"语料段数: {len(r.corpus)}")
    for q in ["音箱怎么连WiFi", "退款要多久到账", "E011 网关离线怎么办",
              "保修期是多久", "它不工作了"]:
        print(f"\nQ: {q}")
        print(f"  rewrite -> {rewrite(q)}")
        for h in r.retrieve(q, top_k=3):
            print(f"  [{h['score']:.3f}] {h['source']} / {h['title']}")
