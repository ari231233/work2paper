"""M28 文档正文证据与关键词边界回归。"""
from __future__ import annotations

from pathlib import Path

from papermine.agents.understand import _build_user_prompt
from papermine.knowledge import _document_excerpt, _kw_count, extract_elements
from papermine.models import Project
from papermine.scanner import scan


def test_english_keyword_uses_token_boundary() -> None:
    assert _kw_count("text", "context contextual") == 0
    assert _kw_count("text", "plain text data") == 1
    assert _kw_count("fault", "default fallback") == 0


def test_paper_excerpt_and_aerospace_signals_reach_evidence(tmp_path: Path) -> None:
    paper = tmp_path / "paper.tex"
    paper.write_text(
        "Online Hybrid Obstacle Avoidance with DAIDALUS Guidance for NASA GUAM "
        "Lift+Cruise eVTOL. Prescribed performance control provides trajectory tracking.",
        encoding="utf-8",
    )
    project = Project(name="paper", root=str(tmp_path), assets=scan(str(tmp_path)))
    element, evidence = extract_elements(project)
    assert "避障与冲突消解" in element.tasks
    assert "轨迹跟踪" in element.tasks
    assert "飞行控制" in element.methods
    assert "航空航天" in element.scenarios
    assert any("Online Hybrid Obstacle Avoidance" in item.snippet for item in evidence)


def test_current_copy_wins_over_identical_archive(tmp_path: Path) -> None:
    (tmp_path / "archive").mkdir()
    content = "NASA GUAM obstacle avoidance manuscript"
    (tmp_path / "archive" / "old.txt").write_text(content, encoding="utf-8")
    (tmp_path / "paper.txt").write_text(content, encoding="utf-8")
    project = Project(name="paper", root=str(tmp_path), assets=scan(str(tmp_path)))
    _, evidence = extract_elements(project)
    excerpts = [item for item in evidence if item.snippet.startswith("文档正文摘录")]
    assert excerpts[0].source == "paper.txt"


def test_document_excerpt_redacts_email_and_credentials() -> None:
    excerpt = _document_excerpt("author@example.com api_key=sk-secret research abstract")
    assert "author@example.com" not in excerpt
    assert "sk-secret" not in excerpt


def test_understand_prompt_bounds_evidence() -> None:
    evidence = [
        {"source": "p{}.pdf".format(index), "snippet": "文档正文摘录：paper {}".format(index)}
        for index in range(20)
    ] + [
        {"source": "k{}.txt".format(index), "snippet": "命中任务（{}）".format(index)}
        for index in range(80)
    ]
    prompt = _build_user_prompt({}, evidence)
    assert prompt.count("文档正文摘录") == 12
    assert prompt.count("命中任务") == 40
