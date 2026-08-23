"""⑦ 经验沉淀 Agent 单测（M8 策略版）：接口契约、去领域化、结构化 policy、effect、降级、落盘。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from papermine import experience, storage
from papermine.agents.reflect import REFLECT_SCHEMA, run
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider

_REQUIRED_KEYS = ("experience_id", "type", "source_domain", "applicability", "principle",
                  "policy", "effect", "confidence", "support_count", "status")


class _FakeLLM:
    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else {}
        self.exc = exc
        self.calls = []

    def complete(self, system, user, schema, temperature=0.3):
        self.calls.append((system, user, schema, temperature))
        if self.exc is not None:
            raise self.exc
        return self.result


def _llm_entry(**overrides):
    base = {
        "source_domain": "工业制造",
        "applicability": {
            "domains": ["工业制造"],
            "task_types": ["异常检测"],
            "preconditions": ["项目包含任务：异常检测"],
        },
        "principle": "建模类任务可抽象成可发表问题",
        "policy": {"target": "evaluation", "directive": "评估 novelty 前先检查机制性创新"},
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def _dossier(accept=False):
    d = Dossier(project_id="proj-m7", llm_backend="deepseek")
    d.meta["run_id"] = "run_m7"
    d.assets["facts"] = {
        "tasks": ["异常检测", "剩余寿命预测"],
        "methods": ["孤立森林", "LSTM"],
        "data": ["时序数据"],
        "scenarios": ["工业制造"],
        "metrics": ["F1"],
        "libraries": ["scikit-learn"],
        "modules": ["DataPipeline"],
    }
    d.assets["narrative"] = "工业设备预测性维护项目。"
    d.problems = [{"problem_id": "p1", "title": "异常检测", "formulation": "如何……"}]
    d.evaluations = [{"idea_ref": "i1", "verdict": "proceed" if accept else "rework"}]
    d.roadmap = {"selected_idea": "i1", "paper_type": "方法论文"}
    d.human_decisions = [{"checkpoint": "cp2", "decision": "accept", "note": ""}] if accept else []
    return d


class ReflectTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()

    def tearDown(self) -> None:
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def test_run_null_llm_writes_at_least_one_entry(self):
        d = _dossier()
        run(d, NullProvider())

        entries = experience.read_semantic()
        self.assertGreaterEqual(len(entries), 1)
        e = entries[0]
        for key in _REQUIRED_KEYS:
            self.assertIn(key, e)
        self.assertEqual(e["type"], "pattern")
        self.assertEqual(e["source_runs"], ["run_m7"])
        self.assertGreater(e["confidence"], 0)
        self.assertGreaterEqual(e["support_count"], 1)
        # 去领域化 principle + 结构化 policy + effect
        self.assertTrue(e["principle"])
        self.assertIn(e["policy"]["target"], experience.TARGETS)
        self.assertTrue(e["policy"]["directive"])
        self.assertIn(e["effect"]["outcome"], experience.OUTCOMES)
        self.assertEqual(d.meta["prompt_versions"]["reflect"], "v2")

    def test_run_accept_decisions_mark_active(self):
        d = _dossier(accept=True)
        run(d, NullProvider())
        entries = experience.read_semantic()
        self.assertGreaterEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "active")
        self.assertEqual(entries[0]["effect"]["outcome"], "positive")

    def test_run_no_accept_stays_candidate(self):
        d = _dossier(accept=False)
        run(d, NullProvider())
        entries = experience.read_semantic()
        self.assertGreaterEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "candidate")
        self.assertEqual(entries[0]["effect"]["outcome"], "neutral")

    def test_run_with_llm_writes_distilled_entries(self):
        d = _dossier()
        llm = _FakeLLM(result={"entries": [
            _llm_entry(),
            _llm_entry(principle="建模类任务可抽象成可发表问题"),  # 重复 principle 去重
        ]})
        run(d, llm)
        entries = experience.read_semantic()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_domain"], "工业制造")
        self.assertEqual(entries[0]["confidence"], 0.9)
        self.assertEqual(entries[0]["source_runs"], ["run_m7"])
        self.assertEqual(entries[0]["policy"]["target"], "evaluation")

    def test_run_llm_error_falls_back_to_deterministic(self):
        d = _dossier()
        run(d, _FakeLLM(exc=LLMError("网络失败")))
        self.assertGreaterEqual(len(experience.read_semantic()), 1)

    def test_run_malformed_llm_falls_back(self):
        d = _dossier()
        run(d, _FakeLLM(result={"entries": "not-a-list"}))
        self.assertGreaterEqual(len(experience.read_semantic()), 1)

    def test_run_empty_entries_falls_back(self):
        d = _dossier()
        run(d, _FakeLLM(result={"entries": []}))
        self.assertGreaterEqual(len(experience.read_semantic()), 1)

    def test_llm_effect_not_self_reported(self):
        """effect 不由 LLM 自评：LLM 输出里带 effect 也会被忽略并确定性填充（§3.8 护栏 #3）。"""
        d = _dossier()
        llm = _FakeLLM(result={"entries": [
            _llm_entry(effect={"outcome": "positive", "measured_by": "llm", "note": "自评"}),
        ]})
        run(d, llm)
        e = experience.read_semantic()[0]
        self.assertEqual(e["effect"]["measured_by"], "human_review")


class SchemaTest(unittest.TestCase):
    def test_schema_requires_entries(self):
        self.assertEqual(REFLECT_SCHEMA["type"], "object")
        self.assertEqual(REFLECT_SCHEMA["required"], ["entries"])
        items = REFLECT_SCHEMA["properties"]["entries"]["items"]
        for key in ("source_domain", "applicability", "principle", "policy", "confidence"):
            self.assertIn(key, items["required"])
        # policy.target 枚举四个行为环节
        self.assertIn(
            "enum", items["properties"]["policy"]["properties"]["target"])


if __name__ == "__main__":
    unittest.main()
