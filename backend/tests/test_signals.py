from __future__ import annotations

import pytest

from core.config import AppConfig
from ingest.landmarks import titles_match
from ingest.signals import SignalExtractor
from ingest.text import normalize_title


@pytest.mark.parametrize(
    ("listed", "actual"),
    [
        ("Batch Normalization", "Batch Normalization: Accelerating Deep Network Training by Reducing Internal Covariate Shift"),  # noqa: E501
        ("GShard", "GShard: Scaling Giant Models with Conditional Computation and Automatic Sharding"),
        ("MIP* = RE", "MIP*=RE"),
        ("DeepSeek-R1", "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning"),
        ("LLM.int8(): 8-bit Matrix Multiplication for Transformers at Scale",
         "LLM.int8(): 8-bit Matrix Multiplication for Transformers at Scale"),
        ("Attention Is All You Need", "Attention is All you Need"),
    ],
)  # fmt: skip
def test_landmark_titles_match(listed: str, actual: str) -> None:
    assert titles_match(listed, actual)


@pytest.mark.parametrize(
    ("listed", "actual"),
    [
        ("Adam: A Method for Stochastic Optimization", "Deep Residual Learning for Image Recognition"),
        ("Mistral 7B", "Mixtral of Experts"),
        ("Layer Normalization", ""),
    ],
)
def test_landmark_titles_mismatch(listed: str, actual: str) -> None:
    assert not titles_match(listed, actual)


def test_normalize_title() -> None:
    assert normalize_title("  Attention Is All\nYou Need! ") == "attentionisallyouneed"


@pytest.fixture
def ex(cfg: AppConfig) -> SignalExtractor:
    return SignalExtractor(cfg)


@pytest.mark.parametrize(
    ("comments", "venue"),
    [
        ("Accepted at NeurIPS 2023", "NeurIPS 2023"),
        ("To appear in the Proceedings of STOC '24", "STOC 24"),
        ("camera-ready version for CVPR", "CVPR"),
        ("Published in Journal of the AMS", "Journal of the AMS"),
        ("Under review at ICLR 2024", None),  # no context word
        ("Accepted. " + "filler " * 15 + "see also ICML 2020 tutorial notes", None),  # context too far away
        ("accepted to NAACL 2022", "NAACL 2022"),
        ("micro-benchmarks, 10 pages", None),
    ],
)
def test_venue_from_comments(ex: SignalExtractor, comments: str, venue: str | None) -> None:
    assert ex.venue_from_comments(comments) == venue


def test_journal_ref_counts_as_accepted(ex: SignalExtractor) -> None:
    s = ex.extract("T", "A", None, "Phys. Rev. D 100, 012345 (2019)")
    assert s.venue_accepted and s.venue_name == "Phys. Rev. D"
    s = ex.extract("T", "A", "irrelevant", "Proceedings of ICML 2021, pp 1-10")
    assert s.venue_name == "ICML 2021"


def test_code_and_flags(ex: SignalExtractor) -> None:
    s = ex.extract("A Benchmark for X", "Code at gitlab.com/foo/bar.", None, None)
    assert s.code_available and s.is_benchmark and not s.is_survey and not s.venue_accepted
    s = ex.extract("Graph methods: an overview", "We study graphs.", "see github.com/x", None)
    assert s.code_available and s.is_survey
    assert not ex.extract("T", "github is great", None, None).code_available
