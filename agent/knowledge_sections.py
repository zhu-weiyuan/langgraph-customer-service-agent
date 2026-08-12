# -*- coding: utf-8 -*-
"""knowledge_sections — 知识库章节解析与黄金小节推导（评测用，只读）。

把 knowledge/*.md 解析为章节列表，并按“黄金要点是否出现在章节文本中”
推导 golden_sections，供 eval/run_real_eval.py 做 section-level 命中判定。
纯函数，不连库、不发请求。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def normalize(text: str) -> str:
    """去空白、去标点（含 = ≥ ≤ > < 等符号），用于宽松匹配。"""
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text)
    compact = re.sub(
        r"[，。！？、；：,.!?;:'\"“”‘’()（）\[\]【】\-—_/\\|=>≥≤<>×·～~≈%％¥￥]",
        "", compact)
    return compact.lower()


def _bigram_overlap_ratio(needle: str, haystack: str) -> float:
    """needle 的二元组在 haystack 中的覆盖率（0~1）。"""
    if len(needle) < 4 or not haystack:
        return 0.0
    nb = {needle[i:i + 2] for i in range(len(needle) - 1)}
    hb = {haystack[i:i + 2] for i in range(len(haystack) - 1)}
    if not nb:
        return 0.0
    return len(nb & hb) / len(nb)


def parse_markdown_sections(md_text: str) -> List[Dict]:
    """按 heading 切分 markdown；无 heading 的正文归入 (doc, intro) 一节。

    Returns: [{"id", "title", "level", "text", "start"}]
    """
    if not md_text:
        return []
    matches = list(HEADING_RE.finditer(md_text))
    sections: List[Dict] = []
    if not matches:
        if md_text.strip():
            sections.append({"id": "intro", "title": "（文档引言）",
                             "level": 0, "text": md_text.strip(), "start": 0})
        return sections
    # 文档大标题(#) 之前的正文
    first = matches[0]
    pre = md_text[: first.start()].strip()
    if pre:
        sections.append({"id": "intro", "title": "（文档引言）",
                         "level": 0, "text": pre, "start": 0})
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
        body = md_text[m.end(): end].strip()
        title = m.group(2).strip()
        sections.append({"id": f"h{idx + 1}", "title": title,
                         "level": len(m.group(1)),
                         "text": body or title, "start": start})
    return sections


@lru_cache(maxsize=8)
def load_sections(source: str, kb_dir: Optional[Path] = None) -> List[Dict]:
    """读取单个知识文件并解析章节。source 为文件名 stem。"""
    kb_dir = Path(kb_dir) if kb_dir else Path(__file__).resolve().parent.parent / "knowledge"
    md = kb_dir / f"{source}.md"
    if not md.is_file():
        return []
    return parse_markdown_sections(md.read_text(encoding="utf-8"))


def load_all_sections(kb_dir: Optional[Path] = None) -> Dict[str, List[Dict]]:
    """source -> [section...]"""
    kb_dir = Path(kb_dir) if kb_dir else Path(__file__).resolve().parent.parent / "knowledge"
    out: Dict[str, List[Dict]] = {}
    for md in sorted(kb_dir.glob("*.md")):
        out[md.stem] = parse_markdown_sections(md.read_text(encoding="utf-8"))
    return out


def derive_golden_sections(item: Dict, kb_dir: Optional[Path] = None) -> List[str]:
    """由 golden_context_ids + key_points/reference_points 推导黄金章节标题。

    只把“要点文本确实出现在该文件某章节”的章节算作黄金章节；
    要点是数值/片段时允许模糊匹配（归一化后包含）。

    **必需要点优先**：若条目带 required_key_points（回答 query 真正必需的
    要点，区别于理想答案里的附加说明），只用它推导 golden_sections。
    避免“附加知识点”把不相关小节拉进 golden 导致 SecHit 误伤。
    """
    sources: Sequence[str] = item.get("golden_context_ids") or item.get("golden_context") or []
    key_points: Sequence[str] = (item.get("required_key_points")
                                 or item.get("key_points")
                                 or item.get("reference_points") or [])
    if not sources or not key_points:
        return []
    hits: List[str] = []
    for kp in key_points:
        kp_norm = normalize(str(kp))
        if not kp_norm:
            continue
        best: Optional[Tuple[float, str]] = None  # (score, src::title)
        for src in sources:
            for sec in load_sections(src, kb_dir):
                sec_norm = normalize(sec.get("text", ""))
                if not sec_norm:
                    continue
                title_norm = normalize(sec.get("title", ""))
                score = 0.0
                if kp_norm and kp_norm in title_norm:
                    # 小节标题直接命中（如错误码 E013 的专属小节）→ 最高
                    score = 3.0
                elif kp_norm in sec_norm:
                    score = 2.0  # 正文精确子串
                else:
                    ratio = _bigram_overlap_ratio(kp_norm, sec_norm)
                    if ratio >= 0.5:
                        score = 0.5 + ratio  # 最高 1.5，永远低于精确匹配
                if score > 0 and (best is None or score > best[0]):
                    best = (score, f"{src}::{sec.get('title', sec.get('id', ''))}")
        if best is not None:
            hits.append(best[1])
    return sorted(set(hits))


def chunk_sections(source: str, chunk_text: str, kb_dir: Optional[Path] = None) -> List[str]:
    """chunk 文本命中的章节标题（chunk 与章节文本有 ≥1 个有效 token 重叠）。"""
    if not chunk_text or not source:
        return []
    chunk_norm = normalize(chunk_text)
    out: List[str] = []
    for sec in load_sections(source, kb_dir):
        sec_norm = normalize(sec.get("text", ""))
        if not sec_norm:
            continue
        # 章节文本较长：取 chunk 归一化串与章节文本做 token 级交集判断
        tokens = {t for t in re.split(r"[\W_]+", chunk_norm) if len(t) >= 2}
        sec_tokens = {t for t in re.split(r"[\W_]+", sec_norm) if len(t) >= 2}
        if tokens & sec_tokens:
            out.append(sec.get("title", sec.get("id", "")))
    return out
