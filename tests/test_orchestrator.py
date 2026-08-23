"""M7 编排器单测：端到端状态机、检查点暂停/续跑、回退、快照、经验沉淀。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from papermine import experience, orchestrator, storage
from papermine.dossier import Dossier
from papermine.llm import NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"


class OrchestratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()
        # 离线：不真调 LLM + 不真联网检索
        self._llm_patch = mock.patch.object(orchestrator, "get_provider", return_value=NullProvider())
        self._retrieval_patch = mock.patch.object(
            orchestrator.ideate, "search_literature", return_value=[])
        self._llm_patch.start()
        self._retrieval_patch.start()

    def tearDown(self) -> None:
        self._retrieval_patch.stop()
        self._llm_patch.stop()
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _run_dir(self, run_id):
        return storage.run_dir(run_id)

    def test_run_pipeline_auto_end_to_end(self):
        run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)
        self.assertTrue(run_id)

        run_dir = self._run_dir(run_id)
        self.assertTrue((run_dir / "dossier.json").exists())
        self.assertTrue((run_dir / "report.md").exists())
        self.assertTrue((run_dir / "report.json").exists())
        self.assertTrue((run_dir / "dossier.history").is_dir())

        dossier = Dossier.load(run_dir)
        self.assertGreaterEqual(len(dossier.problems), 2)
        self.assertGreaterEqual(len(dossier.ideas), 2)
        self.assertGreaterEqual(len(dossier.evaluations), 2)
        self.assertIsNotNone(dossier.roadmap["selected_idea"])
        # auto=True：5 个检查点全部 accept
        self.assertEqual(len(dossier.human_decisions), 5)
        self.assertTrue(all(d["decision"] == "accept" for d in dossier.human_decisions))

        # 结束写出一条经验
        entries = experience.read_semantic()
        self.assertGreaterEqual(len(entries), 1)
        for key in ("confidence", "support_count", "status"):
            self.assertIn(key, entries[0])

        # 状态到 DONE，且每状态迁移都写快照
        st = orchestrator.status(run_id)
        self.assertEqual(st["state"], "DONE")
        self.assertGreaterEqual(len(list((run_dir / "dossier.history").iterdir())), 6)

    def test_status_and_resume_completed_run(self):
        run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)
        st = orchestrator.status(run_id)
        self.assertEqual(st["run_id"], run_id)
        self.assertEqual(st["state"], "DONE")

        # 续跑已完成 run → 仍 DONE，run_id 不变
        resumed = orchestrator.resume(run_id, auto=True)
        self.assertEqual(resumed, run_id)
        self.assertEqual(orchestrator.status(run_id)["state"], "DONE")

    def test_checkpoint_pause_accepts_decision(self):
        decisions = []
        calls = []

        def fake_prompt(checkpoint, label):
            calls.append(checkpoint)
            decisions.append(("accept", "人工确认"))
            return decisions[-1]

        with mock.patch.object(orchestrator, "_prompt", side_effect=fake_prompt):
            run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=False)

        self.assertEqual(calls, ["cp1", "cp2", "cp3", "cp4", "cp5"])
        dossier = Dossier.load(self._run_dir(run_id))
        self.assertEqual(len(dossier.human_decisions), 5)
        self.assertTrue(all(d["decision"] == "accept" for d in dossier.human_decisions))
        self.assertTrue(all(d["note"] == "人工确认" for d in dossier.human_decisions))

    def test_rework_rolls_back_to_prior_state(self):
        calls = []

        def fake_prompt(checkpoint, label):
            calls.append(checkpoint)
            # 第一个检查点 rework 一次 → 回退到 UNDERSTAND 重跑
            if len(calls) == 1:
                return ("rework", "理解有误")
            return ("accept", "")

        with mock.patch.object(orchestrator, "_prompt", side_effect=fake_prompt):
            run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=False)

        st = orchestrator.status(run_id)
        self.assertEqual(st["state"], "DONE")
        self.assertEqual(st["rollback_rounds"].get("cp1"), 1)
        # cp1 rework → 重跑 UNDERSTAND 后再次 cp1，故 checkpoint 总数 = 6
        self.assertEqual(len(calls), 6)
        dossier = Dossier.load(self._run_dir(run_id))
        self.assertTrue(any(d["decision"] == "rework" for d in dossier.human_decisions))


class RollbackTest(unittest.TestCase):
    def test_rollback_respects_max_rounds(self):
        state = {"rollback_rounds": {}}
        for _ in range(orchestrator.MAX_ROLLBACK_ROUNDS):
            self.assertTrue(orchestrator._rollback("cp1", state))
        self.assertFalse(orchestrator._rollback("cp1", state))   # 超限 → 降级
        self.assertEqual(state["rollback_rounds"]["cp1"], orchestrator.MAX_ROLLBACK_ROUNDS)


class ParseDecisionTest(unittest.TestCase):
    def test_parse_decision(self):
        self.assertEqual(orchestrator._parse_decision(""), ("accept", ""))
        self.assertEqual(orchestrator._parse_decision("accept"), ("accept", ""))
        self.assertEqual(orchestrator._parse_decision("y"), ("accept", ""))
        self.assertEqual(orchestrator._parse_decision("接受"), ("accept", ""))
        self.assertEqual(orchestrator._parse_decision("rework"), ("rework", ""))
        self.assertEqual(orchestrator._parse_decision("r"), ("rework", ""))
        self.assertEqual(orchestrator._parse_decision("note 问题不错"), ("note", "问题不错"))
        self.assertEqual(orchestrator._parse_decision("备注 挺好的"), ("note", "挺好的"))
        # 未识别输入 → 接受并附注原文
        self.assertEqual(orchestrator._parse_decision("随便看看"), ("accept", "随便看看"))


class AdvanceTest(unittest.TestCase):
    def test_advance_sequence(self):
        self.assertEqual(orchestrator._advance("UNDERSTAND"), "cp1")
        self.assertEqual(orchestrator._advance("cp1"), "ABSTRACT")
        self.assertEqual(orchestrator._advance("PLAN"), "cp5")
        self.assertEqual(orchestrator._advance("REFLECT"), "DONE")


if __name__ == "__main__":
    unittest.main()
