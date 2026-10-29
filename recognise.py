#!/usr/bin/env python3
"""Recognises a short clip of the stream (testing): Shazam's unofficial API through shazamio, run in the app's
own Python environment (~/.local/share/radio-beat/shazam).

usage: recognise.py CLIP   prints {"artist": ..., "title": ...} (both null when it isn't recognised)"""

import asyncio
import json
import sys

from shazamio import Shazam


async def main(path: str) -> None:
    out = await Shazam().recognize(path)
    track = out.get("track") or {}
    print(json.dumps({"artist": track.get("subtitle"), "title": track.get("title")}))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
