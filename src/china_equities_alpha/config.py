from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _default_data_dir() -> Path:
    return Path(os.environ.get("ALPHA_DATA_DIR", Path.cwd() / "data"))


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=_default_data_dir)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "warehouse.duckdb"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"


@dataclass(frozen=True)
class RunContext:
    """Identity of one ingestion run; every row written in the run shares ``ingested_at``."""

    run_id: str = field(default_factory=lambda: uuid4().hex[:12])
    ingested_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    )


# Small cross-board test universe for development runs (2023-2024 window):
# large caps on each board, 2023 IPOs (first-days-no-limit rule), an ST
# transition with suspensions, and two stocks delisted in 2024.
TEST_UNIVERSE = [
    "600519.SH",  # main board SH
    "000001.SZ",  # main board SZ
    "300750.SZ",  # ChiNext
    "688981.SH",  # STAR
    "601061.SH",  # main board IPO 2023-04-10 (registration reform)
    "688249.SH",  # STAR IPO 2023-05-05
    "301202.SZ",  # ChiNext IPO 2023-07-05
    "000656.SZ",  # becomes ST mid-2023, has suspensions
    "600297.SH",  # delisted 2024-08-28, suspended before delisting
    "600290.SH",  # *ST, delisted 2024-01-16
]
