"""Chart patterns: code track on 1h/4h/1D, vision track via claude-opus-5-5, combined.

python -m app.agents.run_chart [--no-vision] [--save-charts DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.agents import chart_patterns as cp
from app.agents.indicators import candles_frame
from app.agents.run_indicators import load_candles
from app.agents.schema import AgentOutput
from app.config import Config, get_config
from app.data.fetch import data_age
from app.db.session import new_session
from app.llm import Image, load_prompt


async def run(
    cfg: Config, *, vision: bool = True, cycle_id: int | None = None, save_dir: Path | None = None
) -> list[AgentOutput]:
    prompt = load_prompt("chart_patterns")
    outputs = []
    with new_session() as session:
        age = data_age(session, "bybit.candles")
        age_min = int(age.total_seconds() // 60) if age is not None else 10_000
        frames = {
            asset: {
                tf: candles_frame(load_candles(session, cfg.symbol(asset), tf, 300))  # type: ignore[arg-type]
                for tf in cp.TIMEFRAMES
            }
            for asset in cfg.trading.assets
        }
    for asset, by_tf in frames.items():
        code = {tf: cp.code_track(df, tf) for tf, df in by_tf.items() if len(df) >= 60}
        reads = {}
        if vision:
            images = {
                tf: cp.render(df, f"{asset} {cp.TF_LABEL[tf]}")
                for tf, df in by_tf.items()
                if len(df) >= 60
            }
            if save_dir:
                _save(save_dir, asset, images)
            reads = await cp.vision_track(asset, images, prompt, cycle_id)
        outputs.append(cp.combine(asset, code, reads, age_min))
    return outputs


def _save(save_dir: Path, asset: str, images: dict[str, Image]) -> None:
    save_dir.mkdir(parents=True, exist_ok=True)
    for tf, img in images.items():
        (save_dir / f"{asset}_{cp.TF_LABEL[tf]}.png").write_bytes(base64.b64decode(img.data_b64))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-vision", action="store_true", help="code track only, no model calls")
    parser.add_argument("--save-charts", type=Path, help="write the rendered PNGs here")
    args = parser.parse_args(argv)
    print(f"chart_patterns {datetime.now(UTC):%Y-%m-%d %H:%M} UTC")
    for out in asyncio.run(run(get_config(), vision=not args.no_vision, save_dir=args.save_charts)):
        print(f"{out.asset:8s} score {out.score:+.2f}  conf {out.confidence:.2f}")
        for line in out.evidence:
            print(f"    {line}")
        for line in out.risk_flags:
            print(f"    ! {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
