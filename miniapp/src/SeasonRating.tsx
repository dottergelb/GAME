import { useEffect, useState } from "react";
import { apiGet } from "./api";

type Row = { rank: number; uid: number; nickname: string; points: number };
type Resp = { season_id: number | null; rows: Row[] };

export default function SeasonRating() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    setLoading(true);
    apiGet<Resp>("/api/rating/season?limit=100")
      .then((d) => setRows(d.rows ?? []))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="screen-state">Загрузка...</div>;
  if (err) return <div className="screen-state error">Ошибка: {err}</div>;

  return (
    <section>
      <h2 className="screen-title">Рейтинг сезона</h2>
      <div className="list">
        {rows.map((r) => (
          <div key={r.uid} className="row-card">
            <div>
              <div className="nickname">
                #{r.rank} {r.nickname}
              </div>
              <div className="meta">ID: {r.uid}</div>
            </div>
            <div className="points">{r.points}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
