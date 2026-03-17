import { useEffect, useState } from "react";
import { apiGet } from "./api";

type Me = {
  uid: number;
  nickname: string;
  season_points: number;
  matches_played: number;
  wins: number;
  slrpt: number;
  win_mult: number;
  winrate: number;
  verified: boolean;
  game_uid?: string | null;
};

export default function Profile() {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    setLoading(true);
    apiGet<Me>("/api/me")
      .then(setMe)
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="screen-state">Loading...</div>;
  if (err) return <div className="screen-state error">Error: {err}</div>;
  if (!me) return <div className="screen-state">No data</div>;

  return (
    <section>
      <h2 className="screen-title">Profile</h2>

      <div className="stat-card">
        <div className="nickname">{me.nickname}</div>
        <div className="meta">uid: {me.uid}</div>
        <div className="meta">
          Verified: <b>{me.verified ? "Yes" : "No"}</b>
        </div>
        {me.game_uid ? <div className="meta">Game UID: {me.game_uid}</div> : null}
      </div>

      <div className="stat-card">
        <div className="card-title">Season</div>
        <div>Points: {me.season_points}</div>
        <div>Matches: {me.matches_played}</div>
        <div>Wins: {me.wins}</div>
        <div>Winrate: {Math.round(me.winrate * 100)}%</div>
      </div>

      <div className="stat-card">
        <div className="card-title">SLRPT</div>
        <div>SLRPT: {me.slrpt}</div>
        <div>Win multiplier: {me.win_mult.toFixed(2)}</div>
      </div>
    </section>
  );
}
