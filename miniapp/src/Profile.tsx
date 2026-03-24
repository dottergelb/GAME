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

  if (loading) return <div className="screen-state">Загрузка...</div>;
  if (err) return <div className="screen-state error">Ошибка: {err}</div>;
  if (!me) return <div className="screen-state">Нет данных</div>;

  return (
    <section>
      <h2 className="screen-title">Профиль</h2>

      <div className="stat-card">
        <div className="nickname">{me.nickname}</div>
        <div className="meta">ID: {me.uid}</div>
        <div className="meta">
          Верификация: <b>{me.verified ? "Да" : "Нет"}</b>
        </div>
        {me.game_uid ? <div className="meta">Игровой UID: {me.game_uid}</div> : null}
      </div>

      <div className="stat-card">
        <div className="card-title">Сезон</div>
        <div>Очки: {me.season_points}</div>
        <div>Матчи: {me.matches_played}</div>
        <div>Победы: {me.wins}</div>
        <div>Винрейт: {Math.round(me.winrate * 100)}%</div>
      </div>

      <div className="stat-card">
        <div className="card-title">SLRPT</div>
        <div>SLRPT: {me.slrpt}</div>
        <div>Множитель побед: {me.win_mult.toFixed(2)}</div>
      </div>
    </section>
  );
}
