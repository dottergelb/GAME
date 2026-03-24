import { useNavigate } from "react-router-dom";
import { sendTelegramData } from "./telegram";

export default function Dashboard() {
  const navigate = useNavigate();

  return (
    <section>
      <h2 className="screen-title">Control Center</h2>
      <div className="list">
        <article className="row-card">
          <div className="card-title">Matchmaking</div>
          <div className="menu-grid">
            <button
              className="menu-btn"
              type="button"
              onClick={() => {
                const ok = sendTelegramData({ action: "queue_start", platform: "pc" });
                if (!ok) alert("Open mini app from Telegram to send queue actions.");
              }}
            >
              Start Queue PC
            </button>
            <button
              className="menu-btn"
              type="button"
              onClick={() => {
                const ok = sendTelegramData({ action: "queue_start", platform: "android" });
                if (!ok) alert("Open mini app from Telegram to send queue actions.");
              }}
            >
              Start Queue Android
            </button>
            <button className="menu-btn" type="button" onClick={() => sendTelegramData({ action: "queue_cancel" })}>
              Cancel Queue
            </button>
          </div>
        </article>

        <article className="row-card">
          <div className="card-title">Tournaments</div>
          <p className="meta">Creation, requests, judge approvals, deputy founder and moderation.</p>
          <div className="menu-grid">
            <button className="menu-btn" type="button" onClick={() => navigate("/tournaments")}>
              Open Tournament Hub
            </button>
          </div>
        </article>

        <article className="row-card">
          <div className="card-title">Ratings</div>
          <div className="menu-grid">
            <button className="menu-btn" type="button" onClick={() => navigate("/season")}>
              Season Rating
            </button>
            <button className="menu-btn" type="button" onClick={() => navigate("/slrpt")}>
              SLRPT Rating
            </button>
          </div>
        </article>
      </div>
    </section>
  );
}

