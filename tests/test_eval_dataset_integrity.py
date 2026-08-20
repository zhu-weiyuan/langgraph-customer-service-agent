import json
import subprocess
from pathlib import Path

from scripts.ingest_knowledge import knowledge_files
from scripts.validate_golden_set import GoldenSetValidator

ROOT = Path(__file__).resolve().parents[1]

def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]

def test_core_golden_set_excludes_noise_probes():
    core = load_jsonl(ROOT / "eval" / "golden_set_v2.jsonl")
    probes = load_jsonl(ROOT / "eval" / "golden_set_v2_noise_probes.jsonl")
    assert len(core) == 90
    assert not any(row.get("noise_probe") for row in core)
    assert probes and all(row.get("noise_probe") for row in probes)
    assert {row["base_id"] for row in probes} <= {row["id"] for row in core}

def test_hard_set_keeps_historical_ids_for_same_queries():
    current = load_jsonl(ROOT / "eval" / "rag_eval_hard.jsonl")
    raw = subprocess.run(["git", "show", "HEAD:eval/rag_eval_hard.jsonl"],
                         cwd=ROOT, check=True, capture_output=True, text=True,
                         encoding="utf-8").stdout
    historical = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert {r["query"]: r["id"] for r in current} == {r["query"]: r["id"] for r in historical}

def test_knowledge_manifest_excludes_repository_readme():
    files = knowledge_files(ROOT / "knowledge")
    assert all(path.name != "README.md" for path in files)
    assert files

def test_validator_json_output_is_machine_readable(capsys):
    validator = GoldenSetValidator()
    result = validator.validate_v2(ROOT / "eval" / "golden_set_v2.jsonl")
    assert result.error_count == 0
    payload = json.dumps([result.to_dict()], ensure_ascii=False)
    assert json.loads(payload)[0]["total_samples"] == 90
