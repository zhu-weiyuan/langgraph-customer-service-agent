#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Golden Set 验证脚本 — 捕获所有已知 schema 漂移、字段缺失、ID 重复、chunk 存在性等问题

用法：
    python scripts/validate_golden_set.py                    # 校验所有 golden set
    python scripts/validate_golden_set.py --file eval/golden_set_v2.jsonl
    python scripts/validate_golden_set.py --fix-ids          # 自动修复 ID 连续性（仅 rag_eval_hard）
    python scripts/validate_golden_set.py --json             # JSON 输出供 CI 解析
"""

import json
import os
import sys
import argparse
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple, Optional
from collections import Counter, defaultdict
from dataclasses import dataclass, field

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ─── 期望 Schema 定义 ──────────────────────────────────────────────
REQUIRED_FIELDS_V2 = {
    "id", "layer", "category", "difficulty", "question", "query",
    "golden_answer", "golden_context", "golden_context_ids",
    "metadata_filter", "answer_type", "key_points", "reference_points",
    "should_refuse", "weight", "golden_sections", "golden_chunk_ids",
    "intent", "emotion"
}

OPTIONAL_FIELDS_V2 = {"conversation", "required_key_points", "sg", "multi_hop",
                    "base_id", "noise_probe", "injected_noise_chunk_id",
                    "injected_noise_source", "injected_noise_score",
                    "injected_noise_title", "noise_position"}

REQUIRED_FIELDS_RAG_HARD = {
    "id", "tier", "category", "query", "golden_context_ids",
    "golden_section", "expected_keywords", "reference_answer",
    "should_refuse", "weight", "sg", "multi_hop"
}

VALID_LAYERS = {"retrieval", "generation", "agent", "engineering"}
VALID_TIERS = {"normal", "edge", "adversarial", "high"}
VALID_DIFFICULTIES = {"easy", "medium", "hard"}
VALID_ANSWER_TYPES = {"流程说明", "事实问答", "多跳推理", "精确匹配", "对比", "摘要", "拒答"}
VALID_EMOTIONS = {"neutral", "angry", "sad", "anxious", "happy", "frustrated", "urgent", "anger"}
VALID_INTENTS = {"流程说明", "事实问答", "多跳推理", "精确匹配", "对比", "摘要", "拒答", "咨询", "投诉", "闲聊", "结束"}

# ─── 知识库文件映射（用于 chunk 存在性校验） ───────────────────────
KB_FILES = [
    "account-security.md", "api-developer.md", "billing-invoices.md",
    "error-codes.md", "faq.md", "installation-guide.md",
    "product-manual.md", "promotions-membership.md",
    "returns-refunds.md", "shipping-logistics.md",
    "troubleshooting.md", "warranty-service.md"
]

BASE_DIR = Path(__file__).resolve().parent.parent
KB_DIR = BASE_DIR / "knowledge"


# ─── 结果数据结构 ─────────────────────────────────────────────────
@dataclass
class Issue:
    severity: str      # "ERROR" | "WARN" | "INFO"
    file: str
    line: int
    sample_id: str
    field: str
    message: str

    def to_dict(self) -> Dict:
        return {
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "sample_id": self.sample_id,
            "field": self.field,
            "message": self.message
        }


@dataclass
class ValidationResult:
    file: str
    total_samples: int
    issues: List[Issue] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "ERROR")

    @property
    def warn_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "WARN")

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "INFO")

    def to_dict(self) -> Dict:
        return {
            "file": self.file,
            "total_samples": self.total_samples,
            "error_count": self.error_count,
            "warn_count": self.warn_count,
            "info_count": self.info_count,
            "issues": [i.to_dict() for i in self.issues],
            "stats": self.stats
        }


# ─── 工具函数 ─────────────────────────────────────────────────────
def load_jsonl(path: Path) -> List[Tuple[int, Dict]]:
    """返回 [(line_no, obj), ...]"""
    results = []
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                results.append((i, obj))
            except json.JSONDecodeError as e:
                results.append((i, {"_json_error": str(e)}))
    return results


def load_kb_chunks() -> Set[str]:
    """Build the real chunk-id manifest from the production chunker.

    This intentionally does not guess p/c ranges: guessed IDs can make a
    malformed golden reference look valid.
    """
    try:
        from agent.hybrid_rag import chunk_document
    except Exception:
        return set()
    chunks: Set[str] = set()
    for path in sorted(KB_DIR.glob("*.md")):
        if path.name == "README.md" or path.name.startswith("_"):
            continue
        try:
            chunked = chunk_document(
                path.read_text(encoding="utf-8"),
                child_size=300, parent_size=1200, doc_id=path.stem,
            )
        except Exception:
            continue
        chunks.update(chunked.get("parents", {}).keys())
        chunks.update(c.get("child_id") for c in chunked.get("children", [])
                      if c.get("child_id"))
    return chunks


# ─── 核心校验器 ──────────────────────────────────────────────────
class GoldenSetValidator:
    def __init__(self, kb_chunks: Optional[Set[str]] = None):
        self.kb_chunks = kb_chunks or load_kb_chunks()
        self.seen_ids: Set[str] = set()

    def validate_v2(self, path: Path) -> ValidationResult:
        """校验 golden_set_v2.jsonl（generation 核心集）"""
        result = ValidationResult(file=str(path), total_samples=0)
        samples = load_jsonl(path)
        result.total_samples = len(samples)

        id_counter = Counter()
        category_counter = Counter()
        difficulty_counter = Counter()
        emotion_counter = Counter()
        intent_counter = Counter()
        answer_type_counter = Counter()
        has_conversation = 0
        sg_count = 0
        multi_hop_count = 0
        missing_standard_fields = 0

        for line_no, obj in samples:
            if "_json_error" in obj:
                result.issues.append(Issue(
                    "ERROR", str(path), line_no, f"line_{line_no}", "_json",
                    f"JSON 解析失败: {obj['_json_error']}"
                ))
                continue

            sample_id = obj.get("id", f"line_{line_no}")
            id_counter[sample_id] += 1

            # 1. 必填字段（长对话样本可能缺 layer/category/difficulty）
            for field in REQUIRED_FIELDS_V2:
                if field not in obj:
                    result.issues.append(Issue(
                        "ERROR", str(path), line_no, sample_id, field,
                        f"缺失必填字段: {field}"
                    ))
                    missing_standard_fields += 1

            # 2. 字段类型/值域校验
            self._check_field_values(result, path, line_no, sample_id, obj)

            # 3. 结构一致性检查（针对 long-conv-memory 等异常样本）
            self._check_structure_consistency(result, path, line_no, sample_id, obj)

            # 4. Noise probes are validated separately from the core set.
            self._check_noise_probe(result, path, line_no, sample_id, obj)

            # 5. golden_chunk_ids 存在性（软检查：只在 KB 已知模式中）
            self._check_chunk_ids(result, path, line_no, sample_id, obj)

            # 5. metadata_filter 与 golden_context_ids 一致性
            self._check_metadata_filter_consistency(result, path, line_no, sample_id, obj)

            # 6. 拒答题 golden_answer 格式
            if obj.get("should_refuse"):
                self._check_refusal_format(result, path, line_no, sample_id, obj)

            # 7. 统计
            category_counter[obj.get("category", "unknown")] += 1
            difficulty_counter[obj.get("difficulty", "unknown")] += 1
            emotion_counter[obj.get("emotion", "unknown")] += 1
            intent_counter[obj.get("intent", "unknown")] += 1
            answer_type_counter[obj.get("answer_type", "unknown")] += 1
            if "conversation" in obj:
                has_conversation += 1
            if obj.get("sg") is True:
                sg_count += 1
            if obj.get("multi_hop") is True:
                multi_hop_count += 1

        # 8. ID 去重
        for sid, cnt in id_counter.items():
            if cnt > 1:
                result.issues.append(Issue(
                    "ERROR", str(path), 0, sid, "id", f"重复 ID，出现 {cnt} 次"
                ))

        result.stats = {
            "categories": dict(category_counter),
            "difficulties": dict(difficulty_counter),
            "emotions": dict(emotion_counter),
            "intents": dict(intent_counter),
            "answer_types": dict(answer_type_counter),
            "has_conversation": has_conversation,
            "sg_count": sg_count,
            "multi_hop_count": multi_hop_count,
            "unique_ids": len(id_counter),
            "missing_standard_fields": missing_standard_fields
        }
        return result

    def validate_rag_hard(self, path: Path, fix_ids: bool = False) -> ValidationResult:
        """校验 rag_eval_hard.jsonl（检索硬核集）"""
        result = ValidationResult(file=str(path), total_samples=0)
        samples = load_jsonl(path)
        result.total_samples = len(samples)

        id_counter = Counter()
        tier_counter = Counter()
        category_counter = Counter()
        sg_count = 0
        multi_hop_count = 0
        ids_in_order = []

        for line_no, obj in samples:
            if "_json_error" in obj:
                result.issues.append(Issue(
                    "ERROR", str(path), line_no, f"line_{line_no}", "_json",
                    f"JSON 解析失败: {obj['_json_error']}"
                ))
                continue

            sample_id = obj.get("id", f"line_{line_no}")
            id_counter[sample_id] += 1
            ids_in_order.append(sample_id)

            # 必填字段
            for field in REQUIRED_FIELDS_RAG_HARD:
                if field not in obj:
                    result.issues.append(Issue(
                        "ERROR", str(path), line_no, sample_id, field,
                        f"缺失必填字段: {field}"
                    ))

            # 值域校验
            if obj.get("tier") not in VALID_TIERS:
                result.issues.append(Issue(
                    "WARN", str(path), line_no, sample_id, "tier",
                    f"未知 tier: {obj.get('tier')}，有效值: {VALID_TIERS}"
                ))

            if obj.get("sg"):
                sg_count += 1
            if obj.get("multi_hop"):
                multi_hop_count += 1

            tier_counter[obj.get("tier", "unknown")] += 1
            category_counter[obj.get("category", "unknown")] += 1

        # ID 连续性检查（期望 n01, n02, n03...）
        self._check_id_continuity(result, path, ids_in_order)

        # ID 去重
        for sid, cnt in id_counter.items():
            if cnt > 1:
                result.issues.append(Issue(
                    "ERROR", str(path), 0, sid, "id", f"重复 ID，出现 {cnt} 次"
                ))

        result.stats = {
            "tiers": dict(tier_counter),
            "categories": dict(category_counter),
            "sg_count": sg_count,
            "multi_hop_count": multi_hop_count,
            "unique_ids": len(id_counter),
            "id_sequence": ids_in_order
        }
        return result

    def validate_legacy(self, path: Path) -> ValidationResult:
        """校验 golden_set.jsonl（早期四层集）"""
        result = ValidationResult(file=str(path), total_samples=0)
        samples = load_jsonl(path)
        result.total_samples = len(samples)

        layer_counter = Counter()
        category_counter = Counter()
        difficulty_counter = Counter()

        for line_no, obj in samples:
            if "_json_error" in obj:
                result.issues.append(Issue(
                    "ERROR", str(path), line_no, f"line_{line_no}", "_json",
                    f"JSON 解析失败: {obj['_json_error']}"
                ))
                continue

            sample_id = obj.get("id", f"line_{line_no}")
            layer = obj.get("layer", "unknown")
            layer_counter[layer] += 1
            category_counter[obj.get("category", "unknown")] += 1
            difficulty_counter[obj.get("difficulty", "unknown")] += 1

            if layer not in VALID_LAYERS:
                result.issues.append(Issue(
                    "WARN", str(path), line_no, sample_id, "layer",
                    f"未知 layer: {layer}，有效值: {VALID_LAYERS}"
                ))

        result.stats = {
            "layers": dict(layer_counter),
            "categories": dict(category_counter),
            "difficulties": dict(difficulty_counter)
        }
        return result

    # ─── 具体校验方法 ────────────────────────────────────────────
    def _check_field_values(self, result: ValidationResult, path: Path,
                            line_no: int, sample_id: str, obj: Dict):
        # difficulty
        diff = obj.get("difficulty")
        if diff and diff not in VALID_DIFFICULTIES:
            result.issues.append(Issue("WARN", str(path), line_no, sample_id, "difficulty",
                                       f"未知 difficulty: {diff}"))

        # emotion
        emo = obj.get("emotion")
        if emo and emo not in VALID_EMOTIONS:
            result.issues.append(Issue("WARN", str(path), line_no, sample_id, "emotion",
                                       f"未知 emotion: {emo}，有效值: {VALID_EMOTIONS}"))

        # intent
        intent = obj.get("intent")
        if intent and intent not in VALID_INTENTS:
            result.issues.append(Issue("WARN", str(path), line_no, sample_id, "intent",
                                       f"未知 intent: {intent}"))

        # answer_type
        atype = obj.get("answer_type")
        if atype and atype not in VALID_ANSWER_TYPES:
            result.issues.append(Issue("WARN", str(path), line_no, sample_id, "answer_type",
                                       f"未知 answer_type: {atype}，有效值: {VALID_ANSWER_TYPES}"))

        # weight
        w = obj.get("weight")
        if w is not None:
            try:
                wf = float(w)
                if not (0 < wf <= 10):
                    result.issues.append(Issue("WARN", str(path), line_no, sample_id, "weight",
                                               f"weight 超出合理范围 (0,10]: {wf}"))
            except (ValueError, TypeError):
                result.issues.append(Issue("ERROR", str(path), line_no, sample_id, "weight",
                                           f"weight 非数字: {w}"))

        # should_refuse
        sr = obj.get("should_refuse")
        if sr is not None and not isinstance(sr, bool):
            result.issues.append(Issue("ERROR", str(path), line_no, sample_id, "should_refuse",
                                       f"should_refuse 必须是布尔值: {sr}"))

        # key_points / reference_points 必须是列表
        for field in ("key_points", "reference_points", "golden_sections", "golden_chunk_ids",
                      "golden_context_ids", "expected_keywords"):
            val = obj.get(field)
            if val is not None and not isinstance(val, list):
                result.issues.append(Issue("ERROR", str(path), line_no, sample_id, field,
                                           f"{field} 必须是列表，实际: {type(val).__name__}"))

        # conversation 必须是列表且每项含 role/content
        conv = obj.get("conversation")
        if conv is not None:
            if not isinstance(conv, list):
                result.issues.append(Issue("ERROR", str(path), line_no, sample_id, "conversation",
                                           "conversation 必须是列表"))
            else:
                for i, turn in enumerate(conv):
                    if not isinstance(turn, dict) or "role" not in turn or "content" not in turn:
                        result.issues.append(Issue("WARN", str(path), line_no, sample_id,
                                                   f"conversation[{i}]", "缺失 role/content"))

        # metadata_filter 必须是字典
        mf = obj.get("metadata_filter")
        if mf is not None and not isinstance(mf, dict):
            result.issues.append(Issue("ERROR", str(path), line_no, sample_id, "metadata_filter",
                                       "metadata_filter 必须是字典"))

    def _check_structure_consistency(self, result: ValidationResult, path: Path,
                                     line_no: int, sample_id: str, obj: Dict):
        """检测结构不一致：golden_context 类型、reference_points 类型、required_key_points vs key_points"""
        # golden_context 在 v2 中实际上是列表（源文件名列表），这是正常的
        # 但 long-conv-memory 样本的 golden_context 是列表，与其他样本一致 ✓

        # reference_points: 大多数是列表，但 long-conv-memory 样本是字符串
        rp = obj.get("reference_points")
        if rp is not None and not isinstance(rp, list):
            result.issues.append(Issue("ERROR", str(path), line_no, sample_id, "reference_points",
                                       f"reference_points 应为列表，实际: {type(rp).__name__} (样本: {str(rp)[:50]})"))

        # required_key_points 与 key_points 一致性
        rkp = obj.get("required_key_points")
        kp = obj.get("key_points", [])
        if rkp is not None and isinstance(rkp, list) and isinstance(kp, list):
            missing = set(rkp) - set(kp)
            if missing:
                result.issues.append(Issue("WARN", str(path), line_no, sample_id, "required_key_points",
                                           f"required_key_points 中有 key_points 没覆盖: {missing}"))

        # golden_sections vs golden_chunk_ids 一致性
        gs = obj.get("golden_sections", [])
        gcids = obj.get("golden_chunk_ids", [])
        if isinstance(gs, list) and isinstance(gcids, list):
            # 简单检查：每个 section 应该对应至少一个 chunk
            if len(gs) > 0 and len(gcids) == 0:
                result.issues.append(Issue("WARN", str(path), line_no, sample_id, "golden_sections",
                                           "有 golden_sections 但 golden_chunk_ids 为空"))

        # conversation 字段完整性
        conv = obj.get("conversation")
        if conv is not None:
            if not isinstance(conv, list):
                result.issues.append(Issue("ERROR", str(path), line_no, sample_id, "conversation",
                                           "conversation 必须是列表"))
            else:
                for i, turn in enumerate(conv):
                    if not isinstance(turn, dict) or "role" not in turn or "content" not in turn:
                        result.issues.append(Issue("WARN", str(path), line_no, sample_id,
                                                   f"conversation[{i}]", "缺失 role/content"))

        # sg / multi_hop 标注覆盖率提示（INFO 级）
        if obj.get("category") in ("exact-match", "multi-hop") and not obj.get("sg"):
            result.issues.append(Issue("INFO", str(path), line_no, sample_id, "sg",
                                       "exact-match/multi-hop 类样本建议标注 sg=true（语义鸿沟）"))

    def _check_noise_probe(self, result: ValidationResult, path: Path,
                           line_no: int, sample_id: str, obj: Dict):
        is_probe = bool(obj.get("noise_probe"))
        probe_fields = ("base_id", "injected_noise_chunk_id",
                        "injected_noise_source", "noise_position")
        if not is_probe:
            leaked = [f for f in probe_fields if f in obj]
            if leaked:
                result.issues.append(Issue("WARN", str(path), line_no, sample_id,
                                           "noise_probe",
                                           f"非 probe 样本包含 probe 字段: {leaked}"))
            return
        for field_name in probe_fields:
            if field_name not in obj:
                result.issues.append(Issue("ERROR", str(path), line_no, sample_id,
                                           field_name, "noise_probe 缺失字段"))
        noise_id = obj.get("injected_noise_chunk_id")
        if noise_id and self.kb_chunks and noise_id not in self.kb_chunks:
            result.issues.append(Issue("ERROR", str(path), line_no, sample_id,
                                       "injected_noise_chunk_id",
                                       f"噪声 chunk 不存在于当前真实切分: {noise_id}"))
        pos = obj.get("noise_position")
        if not isinstance(pos, int) or pos < 0:
            result.issues.append(Issue("ERROR", str(path), line_no, sample_id,
                                       "noise_position", "必须是非负整数"))

    def _check_chunk_ids(self, result: ValidationResult, path: Path,
                         line_no: int, sample_id: str, obj: Dict):
        chunk_ids = obj.get("golden_chunk_ids", [])
        for cid in chunk_ids:
            if not isinstance(cid, str):
                result.issues.append(Issue("ERROR", str(path), line_no, sample_id,
                                           "golden_chunk_ids[]", f"chunk_id 非字符串: {cid}"))
                continue
            # 简单格式校验：应含 :pN:cN
            if ":p" not in cid or ":c" not in cid:
                result.issues.append(Issue("WARN", str(path), line_no, sample_id,
                                           "golden_chunk_ids[]",
                                           f"chunk_id 格式疑似异常（期望 stem:pN:cN）: {cid}"))
            # 存在性软检查（仅当 KB chunks 已知时）
            if self.kb_chunks and cid not in self.kb_chunks:
                result.issues.append(Issue("INFO", str(path), line_no, sample_id,
                                           "golden_chunk_ids[]",
                                           f"chunk_id 在当前 KB 扫描中未发现（可能 ingest 后才存在）: {cid}"))

    def _check_metadata_filter_consistency(self, result: ValidationResult, path: Path,
                                           line_no: int, sample_id: str, obj: Dict):
        mf = obj.get("metadata_filter", {})
        gcids = obj.get("golden_context_ids", [])
        if isinstance(mf, dict) and "source" in mf and isinstance(gcids, list):
            mf_source = mf["source"]
            if mf_source not in gcids:
                result.issues.append(Issue("WARN", str(path), line_no, sample_id,
                                           "metadata_filter vs golden_context_ids",
                                           f"metadata_filter.source={mf_source} 不在 golden_context_ids={gcids} 中"))

    def _check_refusal_format(self, result: ValidationResult, path: Path,
                              line_no: int, sample_id: str, obj: Dict):
        ans = obj.get("golden_answer", "")
        if not isinstance(ans, str):
            result.issues.append(Issue("ERROR", str(path), line_no, sample_id, "golden_answer",
                                       "拒答题 golden_answer 必须是字符串"))
        elif not ans.startswith("拒绝"):
            result.issues.append(Issue("WARN", str(path), line_no, sample_id, "golden_answer",
                                       f"拒答题建议以「拒绝」开头以便规则匹配: {ans[:50]}"))

    def _check_id_continuity(self, result: ValidationResult, path: Path, ids: List[str]):
        """检查 ID 是否为连续序列（按前缀分组：nXX, eXX, aXX, hXX）"""
        from collections import defaultdict
        groups = defaultdict(list)
        for sid in ids:
            # 匹配 n01, e01, a01, h01 等格式
            if len(sid) >= 3 and sid[0] in "neah" and sid[1:].isdigit():
                groups[sid[0]].append(int(sid[1:]))
        
        for prefix, nums in groups.items():
            nums.sort()
            gaps = []
            for i in range(1, len(nums)):
                if nums[i] - nums[i-1] > 1:
                    gaps.append((nums[i-1], nums[i]))
            if gaps:
                for a, b in gaps:
                    result.issues.append(Issue("ERROR", str(path), 0, f"{prefix}{a}-{prefix}{b}", "id_continuity",
                                               f"ID 不连续 ({prefix}组): 缺失 {prefix}{a+1} 到 {prefix}{b-1}"))
        
        # 文件顺序不是评测语义的一部分。历史数据可能按题型/风险重新编排，
        # 只要 ID 唯一且各前缀无缺号，就不应因行序变化破坏验证。


# ─── 主流程 ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Golden Set 验证器")
    parser.add_argument("--file", help="指定单个文件校验")
    parser.add_argument("--fix-ids", action="store_true", help="自动修复 rag_eval_hard ID 连续性（生成新文件）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--fail-on-warn", action="store_true", help="WARN 视为失败退出")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    files_to_check = []

    if args.file:
        files_to_check = [(Path(args.file), "auto")]
    else:
        files_to_check = [
            (base / "eval" / "golden_set_v2.jsonl", "v2"),
            (base / "eval" / "golden_set_v2_noise_probes.jsonl", "v2"),
            (base / "eval" / "rag_eval_hard.jsonl", "rag_hard"),
            (base / "eval" / "golden_set.jsonl", "legacy"),
        ]

    all_results = []
    total_errors = 0
    total_warns = 0

    for fpath, ftype in files_to_check:
        if not fpath.exists():
            print(f"[SKIP] 不存在: {fpath}")
            continue

        validator = GoldenSetValidator()
        if ftype == "v2" or (ftype == "auto" and "v2" in fpath.name):
            res = validator.validate_v2(fpath)
        elif ftype == "rag_hard" or (ftype == "auto" and "hard" in fpath.name):
            res = validator.validate_rag_hard(fpath, fix_ids=args.fix_ids)
        elif ftype == "legacy" or (ftype == "auto" and "golden_set.jsonl" in fpath.name):
            res = validator.validate_legacy(fpath)
        else:
            print(f"[SKIP] 未知类型: {fpath}")
            continue

        all_results.append(res)
        total_errors += res.error_count
        total_warns += res.warn_count

        # Human-readable output goes to stderr so --json remains machine-readable.
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"📄 {fpath.name} 校验结果", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)
        print(f"  样本数: {res.total_samples}", file=sys.stderr)
        print(f"  ❌ ERROR: {res.error_count}", file=sys.stderr)
        print(f"  ⚠️  WARN:  {res.warn_count}", file=sys.stderr)
        print(f"  ℹ️  INFO:  {res.info_count}", file=sys.stderr)
        print(f"  📊 统计: {json.dumps(res.stats, ensure_ascii=False)}", file=sys.stderr)
        for iss in res.issues[:20]:
            print(f"    {iss.severity} L{iss.line} | {iss.sample_id} | {iss.field} | {iss.message}", file=sys.stderr)

    if args.json:
        print(json.dumps([r.to_dict() for r in all_results], ensure_ascii=False, indent=2))
    else:
        print(f"\n📋 总结: {len(all_results)} 个文件, {total_errors} 个错误, {total_warns} 个警告", file=sys.stderr)

    # 退出码
    if total_errors > 0 or (args.fail_on_warn and total_warns > 0):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()