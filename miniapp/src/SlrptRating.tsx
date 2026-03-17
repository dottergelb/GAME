import { useEffect, useState } from "react";
import { apiGet } from "./api";

type Row = { rank: number; uid: number; nickname: string; points: number };
type Resp = { season_id: number | null; rows: Row[] };

export default function SlrptRating() {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    setLoading(true);
    apiGet<Resp>("/api/rating/slrpt?limit=100")
      .then((d) => setRows(d.rows ?? []))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="screen-state">Loading...</div>;
  if (err) return <div className="screen-state error">Error: {err}</div>;

  return (
    <section>
      <h2 className="screen-title">SLRPT Rating</h2>
      <div className="list">
        {rows.map((r) => (
          <div key={r.uid} className="row-card">
            <div>
              <div className="nickname">
                #{r.rank} {r.nickname}
              </div>
              <div className="meta">uid: {r.uid}</div>
            </div>
            <div className="points">{r.points}</div>
          </div>
        ))}
      </div>
    </section>
  );
}
