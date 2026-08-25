from __future__ import annotations

from conftest import FakeProvider

from agent.summarize import Turn, build_prompt, summarize, trim_to_budget
from agent.tokenizer import count_tokens


def test_count_tokens_is_zero_for_empty_text():
    assert count_tokens("") == 0


def test_count_tokens_grows_with_length():
    assert count_tokens("def train(model):") > 0
    assert count_tokens("a b c d e f g h") > count_tokens("a b")


def test_first_prompt_has_no_prior_summary_section():
    prompt = build_prompt("", [Turn("user", "what is X?"), Turn("assistant", "X is Y.")])

    assert "Existing summary" not in prompt
    assert "what is X?" in prompt
    assert "X is Y." in prompt


def test_later_prompt_carries_the_prior_summary_forward():
    prompt = build_prompt("Discussed X.", [Turn("user", "and Z?"), Turn("assistant", "Z too.")])

    assert "Existing summary:\nDiscussed X." in prompt
    assert "and Z?" in prompt


def test_summarize_returns_the_model_text():
    provider = FakeProvider(answer="The user asked about X; X calls Y.")
    summary, response = summarize(provider, "", [Turn("user", "q"), Turn("assistant", "a")])

    assert summary == "The user asked about X; X calls Y."
    assert response.output_tokens > 0


def test_an_empty_completion_keeps_the_previous_summary():
    # Erasing an accumulated summary is worse than carrying a stale one; a
    # model that returns nothing must not wipe the thread's memory.
    provider = FakeProvider(answer="   ")
    summary, _ = summarize(provider, "Established facts.", [Turn("user", "q")])

    assert summary == "Established facts."


def test_trim_keeps_everything_when_it_fits():
    turns = [Turn("user", "short question"), Turn("assistant", "short answer")]
    assert trim_to_budget(turns, budget=1000) == turns


def test_trim_drops_the_oldest_turns_first():
    turns = [
        Turn("user", "oldest " * 200),
        Turn("assistant", "middle " * 200),
        Turn("user", "newest"),
    ]
    kept = trim_to_budget(turns, budget=50)

    assert kept, "the most recent turn should always survive a realistic budget"
    assert kept[-1].content == "newest"
    assert all("oldest" not in turn.content for turn in kept)


def test_trim_result_stays_within_budget():
    turns = [Turn("user", f"question {i} " * 50) for i in range(10)]
    kept = trim_to_budget(turns, budget=200)

    assert sum(count_tokens(turn.content) for turn in kept) <= 200


def test_trim_returns_nothing_when_even_one_turn_exceeds_the_budget():
    assert trim_to_budget([Turn("user", "word " * 500)], budget=10) == []
