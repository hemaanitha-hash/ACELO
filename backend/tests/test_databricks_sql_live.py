import asyncio
import sys
from pathlib import Path

# Add backend/ to Python path
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.compute_optimization.databricks_reader import (
    DatabricksSQLReader,
)


async def main():
    reader = DatabricksSQLReader()

    rows = await reader.execute(
        """
        SELECT *
        FROM databricks_ws.agent.cluster
        ORDER BY cluster_id
        """
    )

    print(f"\nRows returned: {len(rows)}")

    for row in rows:
        print(row)


if __name__ == "__main__":
    asyncio.run(main())