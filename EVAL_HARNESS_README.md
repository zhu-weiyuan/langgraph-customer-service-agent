# Evaluation Harness - Phase C+

基于 JavaGuide Evaluation Engineering 标准的客服 Agent 评测系统。

## 核心模块

### 1. `tests/eval_harness.py`

实现完整的评测系统，包含：

#### GoldenCase 数据类
```python
@dataclass
class GoldenCase:
    id: str                          # 用例 ID
    input: str                       # 用户输入
    expected_output_schema: dict     # 期望的输出结构
    context: Optional[dict]          # RAG/memory 上下文
    expected_tools: List[str]        # 期望调用的工具
    tags: List[str]                  # 标签：intent_classification, llm_judge 等
    expected_intent: Optional[str]   # 期望的意图
    expected_reply_keywords: List    # 期望的回复关键词
```

#### CustomerServiceScorer - 规则评分器
- `score_intent_accuracy()` - 意图分类准确性
- `score_satisfaction_response()` - 满意度回复评分
- `score_pii_redaction()` - PII 脱敏检测
- `score_response_format()` - 响应格式验证
- `score_escalation_correctness()` - 升级逻辑正确性
- `score_recall()` - 信息召回率
- `score_faithfulness()` - 回答忠实度（无幻觉）
- `score_tool_usage()` - 工具调用正确性

#### LLMJudgeScorer - LLM 自动评判
```python
judge = LLMJudgeScorer(judge_model="qwen3.6-35b")
result = judge.evaluate(
    question="如何退货？",
    answer="请登录账户申请退货...",
    criteria={"helpfulness": "Is answer helpful?", "completeness": "Is answer complete?"}
)
# Returns: {"score": 4.5, "reasoning": "Answer is helpful and complete"}
```

#### EvalRunner - 评测执行器
```python
runner = EvalRunner(task_name="customer_service")
cases = runner.load_golden_set("tests/data/sample_golden_set.json")
results = runner.run_suite(cases, graph=customer_graph)
```

### 2. `tests/data/sample_golden_set.json`

包含 15 个客服场景的标准测试用例：
- 订单查询
- 产品咨询
- 退款请求（高优先级）
- 技术支持
- PII 安全测试
- 问候/结束对话
- 多意图处理
- 注入攻击测试
- 保修咨询
- 运费咨询
- 支付失败
- 账户登录问题
- 促销活动咨询
- 愤怒客户安抚

### 3. `tests/eval_persistence.py`

SQLite 持久化存储评测结果，支持：
- 结果记录与查询
- JSON 导出
- 历史审计追踪

## 测试文件

### `tests/test_eval_harness.py`
- GoldenCase 和 EvalResult 单元测试
- CustomerServiceScorer 所有评分方法测试
- LLMJudgeScorer 基本功能测试
- EvalRunner 集成测试
- 完整评测流程测试

### `tests/test_llm_judge.py`
- LLM judge prompt 有效性测试
- 不同答案类型的分数分布测试
- 边界情况测试（空答案、乱码、离题等）
- 与规则评分器的集成测试
- 不同模型配置测试

### `tests/test_customer_service_eval.py`
- 向后兼容的旧版测试（使用别名方法）

## 运行评测

### 运行所有测试
```bash
cd C:\Users\Administrator\.openclaw\workspace\langgraph-customer-service-agent
python -m pytest tests/test_eval_harness.py tests/test_llm_judge.py -v
```

### 运行评测演示
```bash
python tests/run_eval_demo.py
```

### 在代码中使用
```python
from tests.eval_harness import EvalRunner, GoldenCase, LLMJudgeScorer
from tests.eval_persistence import EvalResultStore

# 创建评测器
runner = EvalRunner(
    task_name="my_eval",
    store=EvalResultStore("eval_results.db"),
    export_path="results.json"
)

# 加载测试集
cases = runner.load_golden_set("tests/data/sample_golden_set.json")

# 添加实际输出（通常来自 graph.invoke）
for case in cases:
    actual = graph.invoke(case.input)
    if case.context is None:
        case.context = {}
    case.context["actual"] = actual

# 运行评测
results = runner.run_suite(cases)

# 查看结果
print(f"Pass rate: {results['pass_rate']:.1%}")
print(f"Average scores: {results['average_scores']}")
print(f"Failed cases: {results['failed_cases']}")
```

## 输出示例

```
============================================================
EVALUATION RESULTS
============================================================
   Total cases:     15
   Passed:          4
   Failed:          11
   Pass rate:       26.7%

[SCORES] Average scores by metric:
   faithfulness    0.33 ###
   format          1.00 ##########
   intent          1.00 ##########
   pii             0.93 #########
```

## 评分标准

- **format**: 输出是否符合期望的 JSON schema
- **intent**: 意图分类是否准确
- **pii**: 是否检测到 PII 泄露
- **recall**: 关键信息点是否覆盖
- **faithfulness**: 回答是否基于上下文（无幻觉）
- **tools**: 是否调用了正确的工具
- **llm_judge**: LLM 对答案质量的整体评分

**通过标准**: 所有分数 >= 0.8

## 扩展指南

### 添加新的测试用例
在 `tests/data/sample_golden_set.json` 中添加：
```json
{
  "id": "my-new-case",
  "input": "用户问题",
  "expected_output_schema": {"required": ["intent", "reply"]},
  "expected_intent": "consult",
  "tags": ["custom_tag"],
  "context": {"key": "value"}
}
```

### 添加自定义评分器
继承 `CustomerServiceScorer` 并添加新方法：
```python
class CustomScorer(CustomerServiceScorer):
    def score_custom_metric(self, output, expected) -> float:
        # 自定义评分逻辑
        return 1.0
```

### 使用不同的 LLM Judge 模型
```python
judge = LLMJudgeScorer(judge_model="gpt-4")  # 或其他模型
```

## 文件清单

```
tests/
├── eval_harness.py              # 核心评测模块
├── eval_persistence.py          # 结果持久化
├── eval_results.db              # SQLite 数据库（运行时生成）
├── data/
│   ├── sample_eval.json         # 旧版示例
│   └── sample_golden_set.json   # 15 个标准测试用例
├── test_eval_harness.py         # 评测系统单元测试
├── test_llm_judge.py            # LLM Judge 测试
├── test_customer_service_eval.py # 向后兼容测试
└── run_eval_demo.py             # 演示脚本
```

## 下一步

1. 集成真实 graph 进行端到端评测
2. 添加更多测试用例覆盖边界情况
3. 配置 CI/CD 自动运行评测
4. 添加评测结果可视化报告
5. 优化 LLM judge prompt 提高评判准确性
