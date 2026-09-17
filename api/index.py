"""The hosted entry point.

The same HTTP surface the local service serves, deployed where the repository is
read-only and no signing tool exists. Two things differ from a local run, and both
are stated rather than hidden:

    read-only     no paying key is configured here, so nothing on this instance
                  signs a burn. Value still moves: the rail finishes transfers that
                  are in flight - including ones somebody else made - through the
                  execution layer. Creating one is done in the caller's own wallet,
                  or by running the rail locally where a key lives.
    /tmp          a hosted bundle cannot be written to, so anything this instance
                  decides is recorded under /tmp and served alongside the receipts
                  that are committed to the repository.

Visitor addresses are not logged: the service keeps the default quiet log unless
ASTRA_HTTP_LOG is set, and nothing here sets it.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for path in (ROOT, os.path.join(ROOT, "service")):
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ.setdefault("ASTRA_READ_ONLY", "1")
os.environ.setdefault("ASTRA_RECEIPTS_DIR", "/tmp/astra-receipts")
os.environ.setdefault("ASTRA_WATCH_DIR", os.path.join(ROOT, "artifacts", "watch"))
# A hosted instance reads less per request than a local one: the window is a span of
# time, so it is still the same hour on every chain, and the per-chain cap keeps the
# first answer inside a serverless time limit.
os.environ.setdefault("ASTRA_PER_DOMAIN", "6")
os.environ.setdefault("ASTRA_MAX_EXAMINED", "12")

import astra_service  # noqa: E402


class handler(astra_service.Handler):  # noqa: N801 - the name the runtime looks for
    """The runtime instantiates this per request; the service does the work."""
