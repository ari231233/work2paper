"""M3 项目理解 Agent 单测：接口契约、确定性 facts、LLM narrative/纠偏、降级路径。

用标准库 unittest 编写，`python -m unittest discover -s tests -v` 可运行（兼容 pytest 收集）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from papermine.agents.understand import (
    UNDERSTAND_SCHEMA,
    _apply_corrections,
    _deterministic_narrative,
    run,
)
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"

_FACTS_KEYS = {"tasks", "methods", "data", "scenarios", "metrics", "libraries", "modules"}


class _StubLLM:
    """可编程 LLM stub：按预设返回结果或抛异常，并记录调用。"""

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {}
        self.error = error
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        if self.error is not None:
            raise self.error
        return self.result


class UnderstandAgentTest(unittest.TestCase):
    def test_run_null_llm_writes_complete_assets(self):
        """无 key 兜底：跑 sample 项目，assets 应有完整 facts + 非空 narrative + 证据。"""
        d = Dossier(project_id="proj-m3", llm_backend="deepseek")
        run(str(SAMPLE_PROJECT), d, NullProvider())

        facts = d.assets["facts"]
        self.assertEqual(set(facts), _FACTS_KEYS)
        self.assertTrue(facts["tasks"])
        self.assertTrue(facts["methods"])
        self.assertTrue(facts["libraries"])
        self.assertTrue(d.assets["narrative"].strip())
        self.assertTrue(d.assets["evidence"])
        for ev in d.assets["evidence"]:
            self.assertIn("source", ev)
            self.assertIn("snippet", ev)
        # 记录 prompt 版本，供可重放
        self.assertEqual(d.meta["prompt_versions"]["understand"], "v1")

    def test_run_with_llm_uses_narrative_and_semantic_correction(self):
        d = Dossier()
        llm = _StubLLM(result={
            "narrative": "这是一个面向工业制造的预测性维护项目。",
            "corrections": {"tasks": ["异常检测"], "methods": ["孤立森林", "LSTM"]},
        })
        run(str(SAMPLE_PROJECT), d, llm)

        self.assertEqual(d.assets["narrative"], "这是一个面向工业制造的预测性维护项目。")
        self.assertEqual(d.assets["facts"]["tasks"], ["异常检测"])
        self.assertEqual(d.assets["facts"]["methods"], ["孤立森林", "LSTM"])
        # 结构类（libraries）不被语义纠偏覆盖，仍保留确定性结果
        self.assertTrue(d.assets["facts"]["libraries"])
        # 证据沿用确定性层
        self.assertTrue(d.assets["evidence"])

    def test_run_llm_error_falls_back_to_deterministic(self):
        """LLMError（网络失败）时降级：facts 完整、narrative 非空、不抛异常。"""
        d = Dossier()
        run(str(SAMPLE_PROJECT), d, _StubLLM(error=LLMError("网络失败")))
        self.assertTrue(d.assets["narrative"].strip())
        self.assertTrue(d.assets["facts"]["tasks"])

    def test_run_llm_bad_output_falls_back(self):
        """LLM 返回不满足 schema 的字段时（靠 stub 模拟），不崩、仍产出完整 assets。"""
        d = Dossier()
        run(str(SAMPLE_PROJECT), d, _StubLLM(result={"corrections": "not-a-dict"}))
        self.assertTrue(d.assets["narrative"].strip())
        self.assertTrue(d.assets["facts"]["tasks"])


class ApplyCorrectionsTest(unittest.TestCase):
    def _facts(self):
        return {
            "tasks": ["异常检测"],
            "methods": ["LSTM"],
            "data": ["时序数据"],
            "scenarios": ["工业制造"],
            "metrics": ["F1"],
            "libraries": ["torch"],
            "modules": ["DataPipeline"],
        }

    def test_empty_corrections_keep_deterministic(self):
        facts = self._facts()
        corrected = _apply_corrections(facts, {"tasks": [], "methods": []})
        self.assertEqual(corrected["tasks"], ["异常检测"])   # 空列表 -> 保留
        self.assertEqual(corrected["methods"], ["LSTM"])

    def test_nonempty_replaces_semantic_only(self):
        facts = self._facts()
        corrected = _apply_corrections(facts, {
            "tasks": ["剩余寿命预测"],
            "libraries": ["numpy"],          # 结构类应被忽略
            "modules": [],
        })
        self.assertEqual(corrected["tasks"], ["剩余寿命预测"])  # 语义类替换
        self.assertEqual(corrected["libraries"], ["torch"])     # 结构类不变
        self.assertEqual(corrected["modules"], ["DataPipeline"])

    def test_corrections_cleaned_and_deduped(self):
        facts = self._facts()
        corrected = _apply_corrections(facts, {
            "methods": [" 孤立森林 ", "孤立森林", "", "LSTM", 123],
        })
        self.assertEqual(corrected["methods"], ["孤立森林", "LSTM"])

    def test_non_dict_corrections_ignored(self):
        facts = self._facts()
        self.assertEqual(_apply_corrections(facts, "nope"), facts)


class DeterministicNarrativeTest(unittest.TestCase):
    def test_narrative_covers_six_tuple(self):
        facts = {
            "tasks": ["异常检测"], "methods": ["孤立森林"], "data": ["时序数据"],
            "scenarios": ["工业制造"], "metrics": ["F1"], "libraries": [], "modules": [],
        }
        n = _deterministic_narrative(facts)
        self.assertIn("工业制造", n)
        self.assertIn("异常检测", n)
        self.assertIn("孤立森林", n)

    def test_narrative_empty_facts_still_nonempty(self):
        empty = {k: [] for k in _FACTS_KEYS}
        self.assertTrue(_deterministic_narrative(empty).strip())


class SchemaTest(unittest.TestCase):
    def test_schema_is_object_with_narrative_and_corrections(self):
        self.assertEqual(UNDERSTAND_SCHEMA["type"], "object")
        self.assertEqual(UNDERSTAND_SCHEMA["required"], ["narrative", "corrections"])
        self.assertIn("narrative", UNDERSTAND_SCHEMA["properties"])
        self.assertIn("corrections", UNDERSTAND_SCHEMA["properties"])


if __name__ == "__main__":
    unittest.main()
