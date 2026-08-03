# -*- coding: utf-8 -*-
"""
PII (Personally Identifiable Information) detection and redaction.

Detects and masks:
- Phone numbers (Chinese format)
- ID card numbers (18-digit)
- Email addresses
- Bank card numbers
"""

import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class PIIFinding:
    pii_type: str
    original: str
    redacted: str
    position: tuple  # (start, end)


@dataclass
class RedactionResult:
    redacted_text: str
    found_pii: List[PIIFinding] = field(default_factory=list)


# ── 业务标识白名单(订单号/工单号/物流单号等不是 PII，误脱敏会让事后
#    排障与回放无法定位具体订单——客服场景这是硬伤) ──────────────
# 命中位置前 _CTX_WINDOW 个字符内若出现这些词，判定为业务标识，跳过脱敏。
_BIZ_CONTEXT_WORDS = (
    "订单", "单号", "订单号", "工单", "运单", "快递", "物流", "流水",
    "交易号", "支付单", "退款单", "编号", "序列号", "SN",
    "order", "orderid", "order_id", "no.", "trackingno", "tracking",
    "ticket", "invoice", "txn", "serial",
)
_CTX_WINDOW = 12  # 往前看多少个字符找业务上下文

# 显式业务前缀(紧邻数字，如 ORD20240115001 / SN12345678901)
_BIZ_PREFIX = re.compile(
    r"(?:ORD|ORDER|NO|SN|TX|TXN|INV|TK|WD)[-_:# ]?$", re.IGNORECASE)


def _is_business_id(text: str, start: int) -> bool:
    """判断 text[start:] 处的数字串是否为业务标识(订单号等)而非 PII。"""
    left = text[max(0, start - _CTX_WINDOW):start]
    low = left.lower()
    if any(w in low for w in _BIZ_CONTEXT_WORDS):
        return True
    return bool(_BIZ_PREFIX.search(left))


# PII detection patterns
_PII_PATTERNS = {
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "bank_card": re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"),
}


def redact(text: str) -> RedactionResult:
    """Detect and redact PII from text.

    Args:
        text: Input text to scan for PII

    Returns:
        RedactionResult with redacted text and list of findings.
    """
    findings = []
    # Process in order: ID card first (longer pattern), then phone, etc.
    order = ["id_card", "phone", "bank_card", "email"]

    for pii_type in order:
        pattern = _PII_PATTERNS[pii_type]
        for match in pattern.finditer(text):
            original = match.group()
            # 纯数字类(手机/身份证/银行卡)可能与订单号等业务标识撞型，
            # 命中业务上下文则跳过——业务标识不是 PII，且脱敏会毁掉可追溯性。
            if pii_type in ("phone", "id_card", "bank_card") and \
                    _is_business_id(text, match.start()):
                continue
            if pii_type == "id_card":
                redacted = original[:6] + "*********" + original[-4:]
            elif pii_type == "phone":
                redacted = original[:3] + "****" + original[-4:]
            elif pii_type == "bank_card":
                digits = re.sub(r'[\s-]', '', original)
                redacted = digits[:6] + "********" + digits[-4:]
            elif pii_type == "email":
                parts = original.split("@")
                redacted = parts[0][:2] + "***@" + parts[1] if len(parts[0]) >= 2 else "***@" + parts[1]
            else:
                redacted = "***"

            findings.append(PIIFinding(
                pii_type=pii_type,
                original=original,
                redacted=redacted,
                position=(match.start(), match.end()),
            ))

    # Apply redactions from end to start to preserve positions
    if findings:
        sorted_findings = sorted(findings, key=lambda f: f.position[0], reverse=True)
        redacted_text = text
        for finding in sorted_findings:
            start, end = finding.position
            redacted_text = redacted_text[:start] + finding.redacted + redacted_text[end:]
    else:
        redacted_text = text

    return RedactionResult(
        redacted_text=redacted_text,
        found_pii=findings,
    )


def scan_and_log(text: str) -> bool:
    """Scan text for PII and log warning if found. Returns True if PII detected."""
    result = redact(text)
    if result.found_pii:
        pii_types = set(f.pii_type for f in result.found_pii)
        print(f"[PII Warning] Detected {len(result.found_pii)} PII items (types: {', '.join(pii_types)})")
        return True
    return False
