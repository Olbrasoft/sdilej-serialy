from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Episode:
    episode_id: int
    series_id: int
    series_title: str
    series_original_title: str | None
    season: int
    number: int
    title: str | None = None
    runtime_min: int | None = None
    description: str = ""
    priority_rank: int | None = None
    priority_score: float = 0.0

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.number:02d}"

    @property
    def identity(self) -> str:
        return f"{self.series_id}:{self.season}:{self.number}"

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Episode":
        return cls(
            episode_id=int(row["episode_id"]),
            series_id=int(row["series_id"]),
            series_title=str(row["series_title"]),
            series_original_title=row.get("series_original_title"),
            season=int(row["season"]),
            number=int(row["episode"]),
            title=row.get("episode_title") or row.get("episode_name"),
            runtime_min=row.get("runtime_min") or row.get("runtime"),
            description=row.get("description") or row.get("series_description") or "",
            priority_rank=row.get("priority_rank"),
            priority_score=float(row.get("priority_score") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["episode"] = data.pop("number")
        data["episode_code"] = self.code
        return data
