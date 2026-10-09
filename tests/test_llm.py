from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from app import llm
from app.llm.base import Image, LLMError, Prompt, Usage, cost_usd


class Out(BaseModel):
    value: int


class FakeProvider:
    name = "fake"

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def complete(self, **kwargs: object) -> tuple[Out, Usage]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise LLMError("boom")
        return Out(value=self.calls), Usage(input_tokens=10, output_tokens=5)


def test_prompt_loader_reads_version_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "x.md").write_text("<!-- version: 3 -->\nBe brief.\n")
    (tmp_path / "bad.md").write_text("no header\n")
    monkeypatch.setattr(llm, "PROMPTS_DIR", tmp_path)
    p = llm.load_prompt("x")
    assert (p.name, p.version, p.system) == ("x", 3, "Be brief.")
    with pytest.raises(ValueError):
        llm.load_prompt("bad")


def test_every_shipped_prompt_has_a_version() -> None:
    for path in llm.PROMPTS_DIR.glob("*.md"):
        assert llm.load_prompt(path.stem).version >= 1


def test_cost_from_pricing_table() -> None:
    pricing = {"m": (Decimal("4.0"), Decimal("20.0"))}
    assert cost_usd(pricing, "m", Usage(1_000_000, 100_000)) == Decimal("6.0")
    assert cost_usd(pricing, "unknown", Usage(5, 5)) == 0


def test_provider_routing_by_model_prefix() -> None:
    with pytest.raises(LLMError):
        llm.provider_for("llama-3")


async def test_complete_retries_once_then_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    logged: list[object] = []
    monkeypatch.setattr(llm, "_log_call", lambda *a, **k: logged.append(a))
    prompt = Prompt("t", 1, "sys")

    once = FakeProvider(fail_times=1)
    monkeypatch.setattr(llm, "provider_for", lambda model: once)
    result = await llm.complete("indicators", Out, prompt=prompt, user_text="q", model="x-1")
    assert result.parsed.value == 2 and result.attempts == 2

    twice = FakeProvider(fail_times=2)
    monkeypatch.setattr(llm, "provider_for", lambda model: twice)
    with pytest.raises(LLMError):
        await llm.complete("indicators", Out, prompt=prompt, user_text="q", model="x-1")
    assert twice.calls == 2
    assert len(logged) == 2  # one success row, one failure row


def test_image_is_plain_data() -> None:
    img = Image(media_type="image/png", data_b64="AAAA")
    assert img.media_type == "image/png"
