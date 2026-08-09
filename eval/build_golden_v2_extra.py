# -*- coding: utf-8 -*-
"""补齐 v2 数据集的 对比 + 摘要 类型样本，追加到 golden_set_v2.jsonl。"""
import json
from pathlib import Path

OUT = Path(r"C:\Users\Administrator\.openclaw\workspace1\langgraph-customer-service-agent\eval\golden_set_v2.jsonl")

new_samples = [
    # ── 对比（comparison）──
    {
        "id": "comparison-01",
        "layer": "generation",
        "category": "comparison",
        "difficulty": "medium",
        "question": "标准快递和加急快递有什么区别？",
        "query": "标准快递和加急快递有什么区别？",
        "golden_answer": "标准快递 2-5 个工作日送达，满 ¥99 免运费、不满收 ¥10，覆盖全国；加急快递 1-2 个工作日送达，运费 ¥15，仅覆盖全国主要城市。偏远地区（新疆、西藏、青海及内蒙部分地区）标准快递 5-8 个工作日，且不支持加急与次日达。",
        "golden_context": ["shipping-logistics"],
        "golden_context_ids": ["shipping-logistics"],
        "metadata_filter": {"source": "shipping-logistics"},
        "answer_type": "对比",
        "key_points": ["标准2-5天/满99免运", "加急1-2天/¥15", "偏远不支持加急"],
        "reference_points": ["标准2-5天/满99免运", "加急1-2天/¥15", "偏远不支持加急"],
        "should_refuse": False,
        "weight": 1.0,
    },
    {
        "id": "comparison-02",
        "layer": "generation",
        "category": "comparison",
        "difficulty": "hard",
        "question": "免费版、家庭版、专业版云存储有什么区别？",
        "query": "免费版、家庭版、专业版云存储有什么区别？",
        "golden_answer": "免费版 5GB、价格 ¥0；家庭版 50GB、¥18/月或 ¥180/年；专业版 500GB、¥38/月或 ¥380/年（年付相当于 10 个月价格）。录像回放期限也不同：免费版 3 天滚动、家庭版 30 天、专业版 90 天。升级套餐立即生效，按旧套餐剩余天数比例折抵新套餐费用。",
        "golden_context": ["billing-invoices", "error-codes"],
        "golden_context_ids": ["billing-invoices", "error-codes"],
        "metadata_filter": {"source": "billing-invoices"},
        "answer_type": "对比",
        "key_points": ["免费5GB/家庭50GB/专业500GB", "回放3/30/90天", "年付10个月价"],
        "reference_points": ["免费5GB/家庭50GB/专业500GB", "回放3/30/90天", "年付10个月价"],
        "should_refuse": False,
        "weight": 1.0,
    },
    {
        "id": "comparison-03",
        "layer": "generation",
        "category": "comparison",
        "difficulty": "hard",
        "question": "G1 和 G2 Pro 网关有什么区别？",
        "query": "G1 和 G2 Pro 网关有什么区别？",
        "golden_answer": "主要区别：子设备容量——G1 上限 64 个，G2 Pro 上限 128 个（X-300 Pro 同样 128 个）；网络接入——G2 Pro 优先插网线（有线回程），G1 依赖 WiFi；备用电池——G2 Pro 内置备用电池，正常断电续航 4 小时，备用电池异常见 E024。两者均支持多网关分流，同账号可添加多个网关。",
        "golden_context": ["error-codes"],
        "golden_context_ids": ["error-codes"],
        "metadata_filter": {"source": "error-codes"},
        "answer_type": "对比",
        "key_points": ["G1=64子设备/G2Pro=128", "G2Pro优先网线", "G2Pro备电4小时"],
        "reference_points": ["G1=64子设备/G2Pro=128", "G2Pro优先网线", "G2Pro备电4小时"],
        "should_refuse": False,
        "weight": 1.0,
    },
    {
        "id": "comparison-04",
        "layer": "generation",
        "category": "comparison",
        "difficulty": "medium",
        "question": "整机保修和付费延保有什么不同？",
        "query": "整机保修和付费延保有什么不同？",
        "golden_answer": "整机保修是随产品附带的：智能音箱整机 1 年、主要部件（扬声器单元、主板、电源适配器）2 年，免费；付费延保是额外购买的延长服务：音箱 ¥299 延至 3 年、¥499 延至 5 年。两者覆盖范围一致（非人为性能故障与部件损坏，不含人为进水、摔碰、自行拆机），延保只是把保修期延长。",
        "golden_context": ["warranty-service"],
        "golden_context_ids": ["warranty-service"],
        "metadata_filter": {"source": "warranty-service"},
        "answer_type": "对比",
        "key_points": ["整机1年免费/延保299延至3年", "覆盖范围一致", "延保仅延长时间"],
        "reference_points": ["整机1年免费/延保299延至3年", "覆盖范围一致", "延保仅延长时间"],
        "should_refuse": False,
        "weight": 1.0,
    },
    # ── 摘要（summary）──
    {
        "id": "summary-01",
        "layer": "generation",
        "category": "summary",
        "difficulty": "easy",
        "question": "总结一下退货流程。",
        "query": "总结一下退货流程。",
        "golden_answer": "退货流程四步：1) 申请——App「我的订单」选对应订单点「申请退货」，质量问题上传照片/视频，无理由退货填原因；2) 审核——客服 1 个工作日内审核，通过后提供退货地址与 RMA 号，未获 RMA 号前勿自行寄回；3) 寄回——寄出并填写快递单号，保留底单，赠品需一并退回；4) 退款——仓库签收验货（1-2 个工作日）通过后 3 个工作日内发起退款。",
        "golden_context": ["returns-refunds"],
        "golden_context_ids": ["returns-refunds"],
        "metadata_filter": {"source": "returns-refunds"},
        "answer_type": "摘要",
        "key_points": ["申请", "审核得RMA", "寄回", "验货后退款"],
        "reference_points": ["申请", "审核得RMA", "寄回", "验货后退款"],
        "should_refuse": False,
        "weight": 1.0,
    },
    {
        "id": "summary-02",
        "layer": "generation",
        "category": "summary",
        "difficulty": "medium",
        "question": "保修政策主要包含哪些内容？概括一下。",
        "query": "保修政策主要包含哪些内容？概括一下。",
        "golden_answer": "保修政策要点：1) 期限——音箱整机 1 年、主要部件 2 年；网关 2 年；传感器 1 年；执行器类（灯泡、插座、窗帘电机）1.5 年；电池属消耗品不保修；2) 范围——覆盖非人为性能故障与部件损坏，不含人为进水、摔碰、自行拆机；3) 申请——App「售后报修」或 400-888-8888，远程诊断 5 分钟，硬件问题免费上门取件；4) 延保——音箱 ¥299 延至 3 年、¥499 延至 5 年。",
        "golden_context": ["warranty-service"],
        "golden_context_ids": ["warranty-service"],
        "metadata_filter": {"source": "warranty-service"},
        "answer_type": "摘要",
        "key_points": ["期限明细", "覆盖范围", "申请方式", "延保"],
        "reference_points": ["期限明细", "覆盖范围", "申请方式", "延保"],
        "should_refuse": False,
        "weight": 1.0,
    },
    {
        "id": "summary-03",
        "layer": "generation",
        "category": "summary",
        "difficulty": "hard",
        "question": "云服务的计费规则都有哪些？帮我梳理一遍。",
        "query": "云服务的计费规则都有哪些？帮我梳理一遍。",
        "golden_answer": "云服务计费规则梳理：1) 套餐——免费 5GB / 家庭 50GB（¥18/月或 ¥180/年）/ 专业 500GB（¥38/月或 ¥380/年），年付等于 10 个月价格；2) 订阅——立即生效按自然周期计费（如 3 月 5 日订阅则每月 5 日扣费），升级按剩余天数折抵、降级周期结束后生效；3) 自动续费——到期自动扣款，扣费前 3 天提醒，可随时关闭；扣费失败第 1、3 天重试，均失败降为免费版；4) 退订——随时退，退款=实付×剩余天数÷总天数，1-7 个工作日原路退回；5) 超量——到期续费降为免费版后超出 5GB 数据保留 30 天供下载。",
        "golden_context": ["billing-invoices"],
        "golden_context_ids": ["billing-invoices"],
        "metadata_filter": {"source": "billing-invoices"},
        "answer_type": "摘要",
        "key_points": ["套餐价格", "订阅周期", "自动续费", "退订退款", "超量处理"],
        "reference_points": ["套餐价格", "订阅周期", "自动续费", "退订退款", "超量处理"],
        "should_refuse": False,
        "weight": 1.0,
    },
]

# 追加（检查 ID 不重复）
existing = [json.loads(l) for l in OUT.open(encoding="utf-8")]
existing_ids = {s["id"] for s in existing}
added = [s for s in new_samples if s["id"] not in existing_ids]

with OUT.open("a", encoding="utf-8") as f:
    for s in added:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")

from collections import Counter
all_rows = existing + added
print(f"追加 {len(added)} 条，总数 {len(all_rows)}")
print("answer_type 分布:", dict(Counter(r["answer_type"] for r in all_rows)))
