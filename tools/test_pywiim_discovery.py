#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from acceptance_pywiim import PywiimAcceptance


async def main() -> int:
    adapter = PywiimAcceptance("127.0.0.1", 80, "Pi Linkplay Podcast", "", 15)
    result = adapter.import_pywiim()
    if not result.ok:
        print(result.detail)
        return 2
    result = await adapter.discover()
    print(result.detail)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
