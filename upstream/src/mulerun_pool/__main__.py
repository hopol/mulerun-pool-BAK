"""Command entry for MuleRun key-pool proxy."""

import os
import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "mulerun_pool.server:app",
        host=os.getenv("MULERUN_POOL_HOST", "0.0.0.0"),
        port=int(os.getenv("MULERUN_POOL_PORT", "8089")),
        reload=os.getenv("MULERUN_POOL_RELOAD", "0").lower() in {"1", "true", "yes"},
    )
