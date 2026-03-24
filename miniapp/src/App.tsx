import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./Dashboard";
import Profile from "./Profile";
import SeasonRating from "./SeasonRating";
import SlrptRating from "./SlrptRating";
import Tournaments from "./Tournaments";
import { getTelegramUser } from "./telegram";
import "./App.css";

export default function App() {
  const tgUser = getTelegramUser();
  const title = tgUser?.username
    ? `@${tgUser.username}`
    : tgUser?.first_name
      ? tgUser.first_name
      : "Мини-приложение";

  return (
    <div className="layout">
      <header className="app-header">
        <div className="app-title">Лига Лёхи</div>
        <div className="app-subtitle">{title}</div>
      </header>

      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/season" element={<SeasonRating />} />
          <Route path="/slrpt" element={<SlrptRating />} />
          <Route path="/tournaments" element={<Tournaments />} />
          <Route path="/me" element={<Profile />} />
        </Routes>
      </main>

      <nav className="bottom-nav">
        <NavLink to="/" end className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          Главная
        </NavLink>
        <NavLink to="/season" className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          Сезон
        </NavLink>
        <NavLink to="/slrpt" className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          SLRPT
        </NavLink>
        <NavLink to="/tournaments" className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          Турниры
        </NavLink>
        <NavLink to="/me" className={({ isActive }) => (isActive ? "tab active" : "tab")}>
          Профиль
        </NavLink>
      </nav>
    </div>
  );
}
