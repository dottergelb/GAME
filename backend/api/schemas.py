from pydantic import BaseModel
from typing import List, Optional


class RatingRow(BaseModel):
    rank: int
    uid: int
    nickname: str
    points: int


class RatingResponse(BaseModel):
    season_id: Optional[int] = None
    rows: List[RatingRow]


class MeResponse(BaseModel):
    uid: int
    nickname: str

    season_points: int
    matches_played: int
    wins: int

    slrpt: int
    win_mult: float
    winrate: float  # 0..1

    verified: bool
    game_uid: Optional[str] = None