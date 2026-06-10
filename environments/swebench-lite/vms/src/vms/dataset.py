from pathlib import Path

import pyarrow.parquet as pq

DEFAULT_DATASET = (
    Path(__file__).resolve().parents[3] / "data" / "files" / "dev.parquet"
)


def load_tasks(dataset: Path) -> list[dict]:
    return pq.read_table(dataset).to_pylist()
