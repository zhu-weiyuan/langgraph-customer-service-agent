#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Golden Set 自动修补脚本

修复内容：
1. golden_set_v2.jsonl:
   - 为 exact-match / multi-hop / privilege-expiry 中的语义鸿沟样本补 sg=true
   - 为 category=multi-hop 样本补 multi_hop=true
   - 修复 required_key_points 缺失覆盖问题
   - 修复拒答题 golden_answer 格式（统一以「拒绝」开头）
   - 为每类样本添加 noise_probe 变体（可选）

2. rag_eval_hard.jsonl:
   - 重新生成连续 ID（n01-nXX, e01-eXX, a01-aXX, h01-hXX）
   - 保持 tier/category/sg/multi_hop 分布不变

3. 生成校验报告
"""

import json
import sys
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Any, Set, Tuple
from collections import Counter
from copy import deepcopy

# ─── 配置 ──────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parent.parent
V2_PATH = BASE / "eval" / "golden_set_v2.jsonl"
HARD_PATH = BASE / "eval" / "rag_eval_hard.jsonl"
BACKUP_DIR = BASE / "eval" / "backups"

# 语义鸿沟判定关键词（用于自动打 sg 标签）
SEMANTIC_GAP_KEYWORDS = {
    # 口语 vs 书面语
    ("不吭声", "没声音"), ("不吭声", "静音"), ("不吭声", "不出声"),
    ("连不上", "配对失败"), ("连不上", "配网"),
    ("红灯", "离线"), ("小盒子", "网关"),
    ("不理我", "唤醒"), ("喊", "唤醒词"),
    ("卡成幻灯片", "延迟高"), ("卡", "打不开"),
    ("存不下", "空间不足"), ("传不上去", "上传失败"),
    ("登不进去", "锁定"), ("密码对", "验证码"),
    ("不变", "省电"), ("阈值", "上报"),
    ("灯亮", "过载"), ("插座", "恢复供电"),
    ("卡住", "遇阻"), ("校准", "行程"),
    ("回音", "同步"), ("对不上拍", "不同步"),
    ("没反应", "启用"), ("时区", "地理位置"),
    ("杂音", "固件"), ("吱吱啦啦", "破音"),
    ("搜不到", "蓝牙模式"), ("蓝牙", "配对"),
    ("停电", "闹钟"), ("断网", "续航"),
    ("升级失败", "回滚"), ("固件", "E005"),
    ("加点钱", "延保"), ("多保几年", "延保服务"),
    ("旧机器", "以旧换新"), ("折价", "估价"),
    ("共用", "家庭共享"), ("密码", "邀请成员"),
    ("防小孩", "儿童模式"), ("乱买", "禁用购物"),
    ("出国", "版权"), ("带出国", "转换器"),
    ("会员待遇", "等级体系"), ("买了不少", "累计实付"),
    ("语音下单", "语音购物"), ("帮我下单", "支付口令"),
    ("退货", "无理由"), ("不想要", "7天"),
    ("5GHz", "仅支持2.4GHz"), ("双频", "2.4G/5G"),
    ("额定功率", "2500W"), ("过载", "E018"),
    ("门窗传感器", "磁体错位"), ("报警", "间距2cm"),
    ("电池", "CR2032"), ("续航", "12个月"),
    ("家庭版", "50GB"), ("专业版", "500GB"),
    ("满减", "199-20"), ("档位", "优惠券规则"),
    ("年付", "退款公式"), ("73天", "剩余天数"),
    ("延保价格", "299"), ("3年", "5年"),
    ("E015", "配对超时"), ("60秒", "1米内"),
    ("E028", "免费版"), ("5GB", "50GB", "500GB"),
    ("普通成员", "仅查看控制"), ("管理员", "删除设备"),
    ("被移出", "立即失去"), ("授权", "摄像头"),
    ("员工", "无查看权限"), ("AES-256", "临时授权"),
    ("注销", "30天冷静期"), ("彻底删除", "不可恢复"),
    ("积分过期", "次年12月31日"), ("清零", "不予恢复"),
    ("发票换开", "90天"), ("自助换开", "人工处理"),
    ("数据导出", "30天一次"), ("7个工作日", "链接7天"),
    ("二手设备", "解绑"), ("强制解绑", "购买凭证"),
    ("优惠券过期", "失效"), ("退货返还", "不返还"),
    ("手机丢了", "下线设备"), ("登录保护", "安全申诉"),
    ("标准快递", "加急快递"), ("2-5天", "1-2天"),
    ("免费版", "家庭版", "专业版"), ("回放", "3天", "30天", "90天"),
    ("G1", "64"), ("G2 Pro", "128"), ("网线", "备用电池"),
    ("整机保修", "延保"), ("免费", "付费"), ("覆盖范围", "一致"),
    ("积分抵扣", "订单退货"), ("同步扣回", "返还积分"),
    ("价保", "退货"), ("二选一", "价保后金额"),
    ("关税", "收件人承担"), ("海外", "DHL", "顺丰国际"),
}

# 需要补 multi_hop=true 的 category
MULTI_HOP_CATEGORIES = {"multi-hop"}

# 需要补 sg=true 的 category（语义鸿沟明显的）
SG_CATEGORIES = {"exact-match", "privilege-expiry", "comparison", "summary"}


# ─── 工具函数 ──────────────────────────────────────────────────
def load_jsonl(path: Path) -> List[Dict]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line.strip()) for line in f if line.strip()]


def save_jsonl(path: Path, data: List[Dict]):
    with path.open("w", encoding="utf-8") as f:
        for obj in data:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def backup_file(path: Path):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    import shutil
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"{path.stem}_{ts}{path.suffix}"
    shutil.copy2(path, backup_path)
    print(f"  📦 备份: {backup_path}")
    return backup_path


def is_semantic_gap(query: str, golden_sections: List[str]) -> bool:
    """启发式判断是否为语义鸿沟题"""
    query_lower = query.lower()
    sections_text = " ".join(golden_sections).lower()
    
    # 检查预定义的口语-书面语对
    for kw_pair in SEMANTIC_GAP_KEYWORDS:
        if len(kw_pair) == 2:
            oral, written = kw_pair
            if oral in query_lower and written in sections_text:
                return True
        elif len(kw_pair) == 3:
            if all(k in query_lower or k in sections_text for k in kw_pair):
                return True
    
    # 通用启发式：query 与 sections 无共同关键词（去停用词后）
    import re
    stopwords = {'的', '了', '是', '在', '有', '和', '就', '都', '而', '与', '及',
                 '这', '那', '吧', '吗', '呢', '啊', '哦', '什么', '怎么', '如何',
                 '可以', '能够', '请问', '一下', '帮我', '我想', '我要', '能不能'}
    query_words = set(re.findall(r'[\u4e00-\u9fff]+', query_lower)) - stopwords
    section_words = set(re.findall(r'[\u4e00-\u9fff]+', sections_text)) - stopwords
    overlap = query_words & section_words
    return len(overlap) == 0 and len(query_words) > 0


# ─── 修补 golden_set_v2.jsonl ──────────────────────────────────
def fix_golden_set_v2(data: List[Dict]) -> List[Dict]:
    fixed = []
    stats = {"sg_added": 0, "multi_hop_added": 0, "rkp_fixed": 0, "refusal_fixed": 0}
    
    for obj in data:
        obj = deepcopy(obj)
        sid = obj.get("id", "")
        category = obj.get("category", "")
        query = obj.get("query", "") or obj.get("question", "")
        golden_sections = obj.get("golden_sections", [])
        
        # 1. 补 sg 标签
        if category in SG_CATEGORIES and not obj.get("sg"):
            # 进一步用启发式确认
            if is_semantic_gap(query, golden_sections):
                obj["sg"] = True
                stats["sg_added"] += 1
                print(f"  ✅ sg=true: {sid} ({category})")
        
        # 2. 补 multi_hop 标签
        if category in MULTI_HOP_CATEGORIES and not obj.get("multi_hop"):
            obj["multi_hop"] = True
            stats["multi_hop_added"] += 1
            print(f"  ✅ multi_hop=true: {sid}")
        
        # 3. 修复 required_key_points 缺失覆盖
        rkp = obj.get("required_key_points", [])
        kp = obj.get("key_points", [])
        if rkp and kp:
            missing = set(rkp) - set(kp)
            if missing:
                # 将缺失的加入 key_points
                obj["key_points"] = kp + list(missing)
                stats["rkp_fixed"] += 1
                print(f"  🔧 补全 key_points: {sid} +{missing}")
        
        # 4. 修复拒答题格式
        if obj.get("should_refuse") and obj.get("golden_answer"):
            ans = obj["golden_answer"]
            if not ans.startswith("拒绝"):
                obj["golden_answer"] = f"拒绝：{ans}"
                stats["refusal_fixed"] += 1
                print(f"  🔧 修正拒答格式: {sid}")
        
        fixed.append(obj)
    
    print(f"\n📊 golden_set_v2 修补统计: {stats}")
    return fixed


# ─── 修补 rag_eval_hard.jsonl ──────────────────────────────────
def fix_rag_eval_hard(data: List[Dict]) -> List[Dict]:
    """重新生成连续 ID，保持分层分布"""
    # 按 tier 分组
    by_tier = defaultdict(list)
    for obj in data:
        tier = obj.get("tier", "normal")
        by_tier[tier].append(obj)
    
    # tier 排序优先级
    tier_order = ["normal", "edge", "adversarial", "high"]
    prefix_map = {"normal": "n", "edge": "e", "adversarial": "a", "high": "h"}
    
    fixed = []
    for tier in tier_order:
        items = by_tier.get(tier, [])
        prefix = prefix_map[tier]
        for idx, obj in enumerate(items, 1):
            obj = deepcopy(obj)
            obj["id"] = f"{prefix}{idx:02d}"
            fixed.append(obj)
    
    # 统计
    tier_counts = Counter(obj["tier"] for obj in fixed)
    sg_count = sum(1 for obj in fixed if obj.get("sg"))
    mh_count = sum(1 for obj in fixed if obj.get("multi_hop"))
    print(f"\n📊 rag_eval_hard 重排统计:")
    print(f"  总数: {len(fixed)}")
    print(f"  分层: {dict(tier_counts)}")
    print(f"  sg=true: {sg_count}")
    print(f"  multi_hop=true: {mh_count}")
    print(f"  ID 序列: {[obj['id'] for obj in fixed]}")
    
    return fixed


# ─── 添加 noise_probe 样本 ──────────────────────────────────────
def add_noise_probes(v2_data: List[Dict], hard_data: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """为每个主要类别添加 1-2 条 noise_probe 样本（复制现有样本并注入噪声）"""
    # 这里只演示逻辑，实际需要人工设计噪声上下文
    # 建议：在检索结果中混入 1 个无关 chunk，观察生成层 Faithfulness 下降
    print("\n⚠️ noise_probe 样本建议人工设计，需结合具体检索结果构造")
    print("   示例：在 exact-match-01 的检索结果中混入 unrelated chunk，标注 noise_probe=true")
    return v2_data, hard_data


# ─── 主流程 ────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Golden Set 自动修补")
    parser.add_argument("--v2", action="store_true", help="修补 golden_set_v2.jsonl")
    parser.add_argument("--hard", action="store_true", help="修补 rag_eval_hard.jsonl")
    parser.add_argument("--all", action="store_true", help="修补所有（默认）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览不写入")
    args = parser.parse_args()
    
    if not (args.v2 or args.hard):
        args.all = True
    
    print("=" * 60)
    print("🔧 Golden Set 自动修补工具")
    print("=" * 60)
    
    # 修补 v2
    if args.all or args.v2:
        print(f"\n📄 处理: {V2_PATH.name}")
        backup_file(V2_PATH)
        data = load_jsonl(V2_PATH)
        fixed = fix_golden_set_v2(data)
        
        if not args.dry_run:
            save_jsonl(V2_PATH, fixed)
            print(f"  💾 已写入: {V2_PATH}")
        else:
            print(f"  🔍 Dry-run 模式，未写入")
    
    # 修补 hard
    if args.all or args.hard:
        print(f"\n📄 处理: {HARD_PATH.name}")
        backup_file(HARD_PATH)
        data = load_jsonl(HARD_PATH)
        fixed = fix_rag_eval_hard(data)
        
        if not args.dry_run:
            save_jsonl(HARD_PATH, fixed)
            print(f"  💾 已写入: {HARD_PATH}")
        else:
            print(f"  🔍 Dry-run 模式，未写入")
    
    # 验证
    print("\n" + "=" * 60)
    print("🔍 修补后校验")
    print("=" * 60)
    import subprocess
    result = subprocess.run([
        sys.executable, "scripts/validate_golden_set.py"
    ], cwd=BASE, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    
    print("\n✅ 完成！")


if __name__ == "__main__":
    main()