"""M4 ② 问题抽象 Agent 单测：契约、LLM 路径、降级路径、prompt 版本化、sample 验收。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from papermine.agents import abstract
from papermine.agents.abstract import PROBLEMS_SCHEMA, run
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider

REQUIRED_FIELDS = ("formulation", "motivation", "why_not_engineering", "evidence_refs")


class _FakeLLM:
    """可编程的 LLMProvider 替身：按 result 返回，或按 exc 抛错。"""

    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        if self.exc is not None:
            raise self.exc
        return self.result


def _dossier() -> Dossier:
    d = Dossier(project_id="proj-1", llm_backend="deepseek")
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
    d.assets["evidence"] = [
        {"source": "README.md", "snippet": "检测设备异常"},
        {"source": "src/model.py", "snippet": "IsolationForest"},
    ]
    return d


class PromptVersionTest(unittest.TestCase):
    def test_prompt_file_has_version_header(self) -> None:
        path = Path(abstract.__file__).resolve().parent.parent / "prompts" / "abstract.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("<!-- version:", text)
        _, version = abstract._load_prompt()
        self.assertEqual(version, "v1")

    def test_run_records_prompt_version(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        self.assertEqual(d.meta["prompt_versions"]["abstract"], "v1")


class LlmPathTest(unittest.TestCase):
    def test_run_llm_path_populates_problems(self) -> None:
        d = _dossier()
        llm = _FakeLLM(result={"problems": [
            {"problem_id": "x", "title": "T1", "formulation": "f1", "motivation": "m1",
             "why_not_engineering": "w1", "evidence_refs": ["README.md", "NOPE.md"]},
            {"problem_id": "x2", "title": "T2", "formulation": "f2", "motivation": "m2",
             "why_not_engineering": "w2", "evidence_refs": []},
        ]})
        run(d, llm)

        self.assertEqual(len(d.problems), 2)
        # 重新编号，保证唯一
        self.assertEqual([p["problem_id"] for p in d.problems], ["p1", "p2"])
        self.assertTrue(all(p["provenance"] == "llm" for p in d.problems))
        # evidence_refs 只保留真实证据源（编造的 NOPE.md 被丢弃）
        self.assertEqual(d.problems[0]["evidence_refs"], ["README.md"])
        for p in d.problems:
            for key in REQUIRED_FIELDS:
                self.assertIn(key, p)

    def test_run_skips_problems_missing_why_not_engineering(self) -> None:
        d = _dossier()
        llm = _FakeLLM(result={"problems": [
            {"title": "ok", "formulation": "f", "motivation": "m",
             "why_not_engineering": "w", "evidence_refs": []},
            {"title": "bad", "formulation": "f", "motivation": "m",
             "why_not_engineering": "", "evidence_refs": []},
        ]})
        run(d, llm)
        self.assertEqual(len(d.problems), 1)
        self.assertEqual(d.problems[0]["provenance"], "llm")

    def test_run_derives_title_when_missing(self) -> None:
        d = _dossier()
        llm = _FakeLLM(result={"problems": [
            {"formulation": "在工业场景下如何对异常检测建模？", "motivation": "m",
             "why_not_engineering": "w", "evidence_refs": []},
        ]})
        run(d, llm)
        self.assertEqual(d.problems[0]["title"], "在工业场景下如何对异常检测建模？")


class FallbackTest(unittest.TestCase):
    def test_run_null_provider_falls_back_to_deterministic(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        self.assertGreaterEqual(len(d.problems), 2)
        for p in d.problems:
            self.assertEqual(p["provenance"], "deterministic")
            self.assertEqual(p["confidence"], "low")
            for key in ("formulation", "motivation", "why_not_engineering"):
                self.assertTrue(p[key])

    def test_run_llm_error_falls_back(self) -> None:
        d = _dossier()
        run(d, _FakeLLM(exc=LLMError("no key")))
        self.assertGreaterEqual(len(d.problems), 2)
        self.assertTrue(all(p["provenance"] == "deterministic" for p in d.problems))

    def test_run_malformed_result_falls_back(self) -> None:
        for result in ({}, {"problems": "not-a-list"}, {"problems": []}):
            d = _dossier()
            run(d, _FakeLLM(result=result))
            self.assertGreaterEqual(len(d.problems), 2, result)
            self.assertTrue(all(p["provenance"] == "deterministic" for p in d.problems))

    def test_run_is_idempotent_replace(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        n = len(d.problems)
        run(d, NullProvider())
        self.assertEqual(len(d.problems), n)


class SchemaTest(unittest.TestCase):
    def test_schema_requires_mandatory_fields(self) -> None:
        required = PROBLEMS_SCHEMA["properties"]["problems"]["items"]["required"]
        for key in REQUIRED_FIELDS:
            self.assertIn(key, required)


class SampleAcceptanceTest(unittest.TestCase):
    """验收：sample 项目跑出 >=2 个带 why_not_engineering 的问题。"""

    def test_sample_project_produces_at_least_two_problems(self) -> None:
        from papermine.knowledge import extract_elements
        from papermine.models import Project
        from papermine.scanner import scan

        root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "examples", "sample-project")
        )
        project = Project(name="sample-project", root=root, assets=scan(root))
        element, evidence = extract_elements(project)

        d = Dossier(project_id="sample", llm_backend="deepseek")
        d.assets["facts"] = {
            "tasks": element.tasks,
            "methods": element.methods,
            "data": element.data,
            "scenarios": element.scenarios,
            "metrics": element.metrics,
            "libraries": element.libraries,
            "modules": element.modules,
        }
        d.assets["narrative"] = "工业设备预测性维护项目。"
        d.assets["evidence"] = [{"source": e.source, "snippet": e.snippet} for e in evidence]

        run(d, NullProvider())

        self.assertGreaterEqual(len(d.problems), 2)
        for p in d.problems:
            self.assertTrue(p["why_not_engineering"])
            self.assertTrue(p["formulation"])
            self.assertTrue(p["motivation"])
            self.assertIsInstance(p["evidence_refs"], list)


if __name__ == "__main__":
    unittest.main()
