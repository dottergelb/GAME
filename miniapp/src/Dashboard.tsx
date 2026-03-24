import { useNavigate } from "react-router-dom";
import { sendTelegramData } from "./telegram";

export default function Dashboard() {
  const navigate = useNavigate();

  return (
    <section>
      <h2 className="screen-title">Центр управления</h2>
      <div className="list">
        <article className="row-card">
          <div className="card-title">Подбор матчей</div>
          <div className="menu-grid">
            <button
              className="menu-btn"
              type="button"
              onClick={() => {
                const ok = sendTelegramData({ action: "queue_start", platform: "pc" });
                if (!ok) alert("Открой мини-приложение из Telegram, чтобы отправлять команды очереди.");
              }}
            >
              Начать очередь (PC)
            </button>
            <button
              className="menu-btn"
              type="button"
              onClick={() => {
                const ok = sendTelegramData({ action: "queue_start", platform: "android" });
                if (!ok) alert("Открой мини-приложение из Telegram, чтобы отправлять команды очереди.");
              }}
            >
              Начать очередь (Android)
            </button>
            <button className="menu-btn" type="button" onClick={() => sendTelegramData({ action: "queue_cancel" })}>
              Отменить очередь
            </button>
          </div>
        </article>

        <article className="row-card">
          <div className="card-title">Турниры</div>
          <p className="meta">Создание, заявки, решения судьи, заместитель основателя и модерация.</p>
          <div className="menu-grid">
            <button className="menu-btn" type="button" onClick={() => navigate("/tournaments")}>
              Открыть центр турниров
            </button>
          </div>
        </article>

        <article className="row-card">
          <div className="card-title">Рейтинги</div>
          <div className="menu-grid">
            <button className="menu-btn" type="button" onClick={() => navigate("/season")}>
              Рейтинг сезона
            </button>
            <button className="menu-btn" type="button" onClick={() => navigate("/slrpt")}>
              Рейтинг SLRPT
            </button>
          </div>
        </article>
      </div>
    </section>
  );
}
