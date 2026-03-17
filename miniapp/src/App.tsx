import { NavLink, Route, Routes } from "react-router-dom";
import Profile from "./Profile";
import SeasonRating from "./SeasonRating";
import SlrptRating from "./SlrptRating";
import { getTelegramUser } from "./telegram";
import "./App.css";

export default function App() {
  const tgUser = getTelegramUser();
  const title = tgUser?.username
    ? `@${tgUser.username}`
    : tgUser?.first_name
      ? tgUser.first_name
      : "Mini App";

  return (
    <div className="layout">
      <header className="app-header">
        <div className="app-title">Leha League</div>
        <div className="app-subtitle">{title}</div>
      </header>

      <main className="content">
        <Routes>
          <Route path="/" element={<SeasonRating />} />
          <Route path="/slrpt" element={<SlrptRating />} />
          <Route path="/me" element={<Profile />} />
        </Routes>
      </main>

      <nav className="bottom-nav">
        <NavLink to="/" end className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          Season
        </NavLink>
        <NavLink to="/slrpt" className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          SLRPT
        </NavLink>
        <NavLink to="/me" className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          Profile
        </NavLink>
      </nav>
    </div>
  );
}
