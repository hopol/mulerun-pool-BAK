"""Command entry for Outlook-based MuleRun registration."""

import asyncio
from .register import main

if __name__ == "__main__":
    asyncio.run(main())
