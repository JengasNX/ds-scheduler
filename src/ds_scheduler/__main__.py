"""Entry point: python -m ds_scheduler"""
from __future__ import annotations

import logging

from . import config
from .bot import run


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("bot.log", encoding="utf-8"),
        ],
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    run(config.token())


if __name__ == "__main__":
    main()
