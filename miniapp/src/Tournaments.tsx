import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, apiPost } from "./api";
import { getTelegramUser, sendTelegramData } from "./telegram";

type TabKey = "create" | "my" | "judge" | "requests";
type Capabilities = {
  can_create_tournament: boolean;
  can_set_deputy: boolean;
  can_judge_panel: boolean;
  can_manage_requests: boolean;
};

type MatchRow = {
  match_id: number;
  tournament_id: number;
  round_name: string;
  scheduled_at: string | null;
  player1_id: number | null;
  player2_id: number | null;
};

type ReplacementRequest = {
  id: number;
  tournament_id: number;
  match_id: number;
  out_user_id: number;
  in_user_id: number;
  reason: string;
  created_at: string;
};

type NickCheckRequest = {
  id: number;
  tournament_id: number;
  user_id: number;
  requested_nickname: string;
  created_at: string;
};

type JudgeReplacementRequest = ReplacementRequest & { created_by: number };
type JudgeNickCheckRequest = NickCheckRequest & { created_by: number };

type RejectTarget = {
  kind: "replacement" | "nickname";
  requestId: number;
};

export default function Tournaments() {
  const founderId = 5538733181;
  const tgUser = getTelegramUser();
  const isFounderByTelegram = tgUser?.id === founderId;

  const [activeTab, setActiveTab] = useState<TabKey>("create");
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [matches, setMatches] = useState<MatchRow[]>([]);
  const [openRep, setOpenRep] = useState<ReplacementRequest[]>([]);
  const [openNick, setOpenNick] = useState<NickCheckRequest[]>([]);
  const [judgeRep, setJudgeRep] = useState<JudgeReplacementRequest[]>([]);
  const [judgeNick, setJudgeNick] = useState<JudgeNickCheckRequest[]>([]);
  const [syncInfo, setSyncInfo] = useState<{ pending_jobs: number; done_jobs: number } | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities>({
    can_create_tournament: false,
    can_set_deputy: false,
    can_judge_panel: false,
    can_manage_requests: true,
  });

  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const [replacementForm, setReplacementForm] = useState({
    matchId: "",
    outUserId: "",
    inUserId: "",
    reason: "игрок отключился",
  });
  const [nickForm, setNickForm] = useState({ tournamentId: "", nickname: "" });
  const [deputyForm, setDeputyForm] = useState({ tournamentId: "", deputyUserId: "" });
  const [createForm, setCreateForm] = useState({
    title: "",
    startDate: "",
    endDate: "",
    formatType: "league",
    maxPlayers: "16",
    matchDays: "0,2,4",
    matchTimes: "18:00,19:00",
    gamesPerDay: "2",
    prizePoolRub: "0",
    judges: "",
  });

  const [rejectTarget, setRejectTarget] = useState<RejectTarget | null>(null);
  const [rejectReason, setRejectReason] = useState("несоответствие правилам");

  const resetMessages = useCallback(() => {
    setError("");
    setSuccess("");
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    resetMessages();
    try {
      const capsPromise = apiGet<Capabilities>("/api/tournaments/capabilities").catch(() => ({
        can_create_tournament: isFounderByTelegram,
        can_set_deputy: isFounderByTelegram,
        can_judge_panel: false,
        can_manage_requests: false,
      }));

      const [caps, m, r, s] = await Promise.all([
        capsPromise,
        apiGet<{ rows: MatchRow[] }>("/api/tournaments/my-matches"),
        apiGet<{ replacement_requests: ReplacementRequest[]; nickname_checks: NickCheckRequest[] }>(
          "/api/tournaments/my-open-requests",
        ),
        apiGet<{ pending_jobs: number; done_jobs: number }>("/api/tournaments/sync-status"),
      ]);
      let j: { replacement_requests: JudgeReplacementRequest[]; nickname_checks: JudgeNickCheckRequest[] } = {
        replacement_requests: [],
        nickname_checks: [],
      };
      if (caps.can_judge_panel) {
        j = await apiGet<{ replacement_requests: JudgeReplacementRequest[]; nickname_checks: JudgeNickCheckRequest[] }>(
          "/api/tournaments/judge/open-requests",
        );
      }
      setCapabilities({
        ...caps,
        can_create_tournament: caps.can_create_tournament || isFounderByTelegram,
        can_set_deputy: caps.can_set_deputy || isFounderByTelegram,
      });
      setMatches(m.rows);
      setOpenRep(r.replacement_requests);
      setOpenNick(r.nickname_checks);
      setSyncInfo(s);
      setJudgeRep(j.replacement_requests);
      setJudgeNick(j.nickname_checks);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось загрузить турниры");
    } finally {
      setLoading(false);
    }
  }, [isFounderByTelegram, resetMessages]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const canSubmit = useMemo(() => !loading && !submitting, [loading, submitting]);
  const tabs = useMemo(() => {
    const out: Array<{ key: TabKey; label: string; desc: string }> = [];
    if (capabilities.can_create_tournament || capabilities.can_set_deputy) {
      out.push({ key: "create", label: "Управление", desc: "Создание турнира и назначение заместителя." });
    }
    if (matches.length > 0) {
      out.push({ key: "my", label: "Мои матчи", desc: "Ваши активные матчи и расписание." });
    }
    if (capabilities.can_manage_requests || openRep.length > 0 || openNick.length > 0) {
      out.push({ key: "requests", label: "Мои заявки", desc: "Замены и проверка никнейма." });
    }
    if (capabilities.can_judge_panel) {
      out.push({ key: "judge", label: "Судейство", desc: "Рассмотрение заявок игроков." });
    }
    return out;
  }, [capabilities, matches.length, openNick.length, openRep.length]);

  useEffect(() => {
    if (!tabs.length) return;
    const exists = tabs.some((t) => t.key === activeTab);
    if (!exists) setActiveTab(tabs[0].key);
  }, [activeTab, tabs]);

  const activeTabMeta = tabs.find((t) => t.key === activeTab);

  const submitReplacement = useCallback(async () => {
    resetMessages();
    const matchId = Number(replacementForm.matchId);
    const outUserId = Number(replacementForm.outUserId);
    const inUserId = Number(replacementForm.inUserId);
    const reason = replacementForm.reason.trim();
    if (!matchId || !outUserId || !inUserId || reason.length < 3) {
      setError("Заполните форму замены корректно.");
      return;
    }
    setSubmitting(true);
    try {
      await apiPost("/api/tournaments/replacement-requests", {
        match_id: matchId,
        out_user_id: outUserId,
        in_user_id: inUserId,
        reason,
      });
      setSuccess("Заявка на замену создана.");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось создать заявку на замену");
    } finally {
      setSubmitting(false);
    }
  }, [replacementForm, refresh, resetMessages]);

  const submitNickCheck = useCallback(async () => {
    resetMessages();
    const tournamentId = Number(nickForm.tournamentId);
    const nickname = nickForm.nickname.trim();
    if (!tournamentId || nickname.length < 3) {
      setError("Заполните форму проверки ника корректно.");
      return;
    }
    setSubmitting(true);
    try {
      await apiPost("/api/tournaments/nickname-checks", {
        tournament_id: tournamentId,
        nickname,
      });
      setSuccess("Заявка на проверку ника создана.");
      setNickForm((prev) => ({ ...prev, nickname: "" }));
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось создать заявку на проверку ника");
    } finally {
      setSubmitting(false);
    }
  }, [nickForm, refresh, resetMessages]);

  const submitDeputy = useCallback(async () => {
    resetMessages();
    const tournamentId = Number(deputyForm.tournamentId);
    const deputyUserId = Number(deputyForm.deputyUserId);
    if (!tournamentId || !deputyUserId) {
      setError("Заполните форму заместителя корректно.");
      return;
    }
    setSubmitting(true);
    try {
      await apiPost("/api/tournaments/deputy", {
        tournament_id: tournamentId,
        deputy_user_id: deputyUserId,
      });
      setSuccess("Заместитель основателя назначен.");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось назначить заместителя");
    } finally {
      setSubmitting(false);
    }
  }, [deputyForm, refresh, resetMessages]);

  const submitCreateTournament = useCallback(async () => {
    resetMessages();
    if (!createForm.title.trim()) {
      setError("Название турнира обязательно.");
      return;
    }
    const matchDays = createForm.matchDays
      .split(",")
      .map((x) => Number(x.trim()))
      .filter((x) => Number.isFinite(x));
    const matchTimes = createForm.matchTimes
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    const judges = createForm.judges
      .split(",")
      .map((x) => Number(x.trim()))
      .filter((x) => Number.isFinite(x));

    setSubmitting(true);
    try {
      await apiPost("/api/tournaments/create", {
        title: createForm.title.trim(),
        start_date: createForm.startDate.trim(),
        end_date: createForm.endDate.trim(),
        format_type: createForm.formatType,
        max_players: Number(createForm.maxPlayers || "16"),
        match_days: matchDays.length ? matchDays : [0, 2, 4],
        match_times: matchTimes.length ? matchTimes : ["18:00", "19:00"],
        games_per_day: Number(createForm.gamesPerDay || "2"),
        prize_pool_rub: Number(createForm.prizePoolRub || "0"),
        judges,
      });
      setSuccess("Турнир создан и отправлен на модерацию (ожидает подтверждения).");
      setCreateForm((prev) => ({ ...prev, title: "" }));
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось создать турнир");
    } finally {
      setSubmitting(false);
    }
  }, [createForm, refresh, resetMessages]);

  const judgeApproveReplacement = useCallback(
    async (requestId: number) => {
      resetMessages();
      setSubmitting(true);
      try {
        await apiPost(`/api/tournaments/judge/replacement/${requestId}/approve`, {});
        setSuccess(`Заявка на замену #${requestId} одобрена.`);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Не удалось одобрить заявку на замену");
      } finally {
        setSubmitting(false);
      }
    },
    [refresh, resetMessages],
  );

  const judgeApproveNick = useCallback(
    async (requestId: number) => {
      resetMessages();
      setSubmitting(true);
      try {
        await apiPost(`/api/tournaments/judge/nickname/${requestId}/approve`, {});
        setSuccess(`Заявка на ник #${requestId} одобрена.`);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Не удалось одобрить заявку на ник");
      } finally {
        setSubmitting(false);
      }
    },
    [refresh, resetMessages],
  );

  const submitReject = useCallback(async () => {
    if (!rejectTarget) return;
    const reason = rejectReason.trim();
    if (!reason) {
      setError("Причина отклонения обязательна.");
      return;
    }
    resetMessages();
    setSubmitting(true);
    try {
      if (rejectTarget.kind === "replacement") {
        await apiPost(`/api/tournaments/judge/replacement/${rejectTarget.requestId}/reject`, { reason });
      } else {
        await apiPost(`/api/tournaments/judge/nickname/${rejectTarget.requestId}/reject`, { reason });
      }
      setSuccess(`Заявка #${rejectTarget.requestId} (${rejectTarget.kind === "replacement" ? "замена" : "ник"}) отклонена.`);
      setRejectTarget(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Не удалось отклонить заявку");
    } finally {
      setSubmitting(false);
    }
  }, [rejectReason, rejectTarget, refresh, resetMessages]);

  return (
    <section>
      <h2 className="screen-title">Турниры</h2>

      <article className="row-card">
        <div className="card-title">Управление</div>
        <p className="meta section-lead">
          Показываются только разделы, доступные для вашей роли.
        </p>
        <div className="segment-tabs">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              type="button"
              className={activeTab === tab.key ? "segment-btn active" : "segment-btn"}
              onClick={() => setActiveTab(tab.key)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {activeTabMeta ? <p className="meta menu-help">{activeTabMeta.desc}</p> : null}
        <div className="menu-grid" style={{ marginTop: 8 }}>
          <button className="menu-btn" type="button" onClick={() => void refresh()} disabled={!canSubmit}>
            Обновить
          </button>
          <button
            className="menu-btn"
            type="button"
            onClick={() => {
              const ok = sendTelegramData({ action: "queue_start", platform: "pc" });
              setSuccess(ok ? "Команда старта очереди (PC) отправлена боту." : "Откройте мини-приложение внутри Telegram для управления очередью.");
            }}
            disabled={!canSubmit}
          >
            Старт очереди (PC)
          </button>
          <button
            className="menu-btn"
            type="button"
            onClick={() => {
              const ok = sendTelegramData({ action: "queue_start", platform: "android" });
              setSuccess(ok ? "Команда старта очереди (Android) отправлена боту." : "Откройте мини-приложение внутри Telegram для управления очередью.");
            }}
            disabled={!canSubmit}
          >
            Старт очереди (Android)
          </button>
          <button
            className="menu-btn"
            type="button"
            onClick={() => {
              const ok = sendTelegramData({ action: "queue_cancel" });
              setSuccess(ok ? "Команда отмены очереди отправлена боту." : "Откройте мини-приложение внутри Telegram для управления очередью.");
            }}
            disabled={!canSubmit}
          >
            Отмена очереди
          </button>
        </div>
        {loading ? <p className="meta">Загрузка...</p> : null}
        {error ? <p className="meta form-error">{error}</p> : null}
        {success ? <p className="meta form-success">{success}</p> : null}
        {syncInfo ? (
          <p className="meta">
            Очередь синхронизации: в ожидании {syncInfo.pending_jobs}, выполнено {syncInfo.done_jobs}
          </p>
        ) : null}
      </article>

      <div className="list" style={{ marginTop: 10 }}>
        {activeTab === "create" ? (
          <>
            {capabilities.can_create_tournament ? (
              <article className="row-card">
                <div className="card-title">Создать турнир (основатель)</div>
                <p className="meta section-lead">Доступно только основателю проекта.</p>
                <div className="form-grid">
                  <input
                    className="form-input"
                    type="text"
                    placeholder="название турнира"
                    value={createForm.title}
                    onChange={(e) => setCreateForm((prev) => ({ ...prev, title: e.target.value }))}
                  />
                  <div className="menu-grid">
                    <input
                      className="form-input"
                      type="text"
                      placeholder="дата начала (ДД.ММ.ГГГГ)"
                      value={createForm.startDate}
                      onChange={(e) => setCreateForm((prev) => ({ ...prev, startDate: e.target.value }))}
                    />
                    <input
                      className="form-input"
                      type="text"
                      placeholder="дата конца (ДД.ММ.ГГГГ)"
                      value={createForm.endDate}
                      onChange={(e) => setCreateForm((prev) => ({ ...prev, endDate: e.target.value }))}
                    />
                  </div>
                  <div className="menu-grid">
                    <select
                      className="form-input"
                      value={createForm.formatType}
                      onChange={(e) => setCreateForm((prev) => ({ ...prev, formatType: e.target.value }))}
                    >
                      <option value="league">Лига</option>
                      <option value="playoff">Плей-офф</option>
                    </select>
                    <input
                      className="form-input"
                      type="number"
                      placeholder="макс. игроков"
                      value={createForm.maxPlayers}
                      onChange={(e) => setCreateForm((prev) => ({ ...prev, maxPlayers: e.target.value }))}
                    />
                  </div>
                  <input
                    className="form-input"
                    type="text"
                    placeholder="дни матчей (например: 0,2,4)"
                    value={createForm.matchDays}
                    onChange={(e) => setCreateForm((prev) => ({ ...prev, matchDays: e.target.value }))}
                  />
                  <input
                    className="form-input"
                    type="text"
                    placeholder="время матчей (например: 18:00,19:00)"
                    value={createForm.matchTimes}
                    onChange={(e) => setCreateForm((prev) => ({ ...prev, matchTimes: e.target.value }))}
                  />
                  <div className="menu-grid">
                    <input
                      className="form-input"
                      type="number"
                      placeholder="игр в день"
                      value={createForm.gamesPerDay}
                      onChange={(e) => setCreateForm((prev) => ({ ...prev, gamesPerDay: e.target.value }))}
                    />
                    <input
                      className="form-input"
                      type="number"
                      placeholder="призовой фонд (руб)"
                      value={createForm.prizePoolRub}
                      onChange={(e) => setCreateForm((prev) => ({ ...prev, prizePoolRub: e.target.value }))}
                    />
                  </div>
                  <input
                    className="form-input"
                    type="text"
                    placeholder="ID судей через запятую"
                    value={createForm.judges}
                    onChange={(e) => setCreateForm((prev) => ({ ...prev, judges: e.target.value }))}
                  />
                  <button className="menu-btn" type="button" onClick={() => void submitCreateTournament()} disabled={!canSubmit}>
                    Создать турнир
                  </button>
                </div>
              </article>
            ) : (
              <article className="row-card">
                <div className="card-title">Создание турнира</div>
                <p className="meta empty-note">Недоступно. Турниры может создавать только основатель.</p>
              </article>
            )}

            {capabilities.can_set_deputy ? (
              <article className="row-card">
                <div className="card-title">Назначить заместителя основателя</div>
                <p className="meta section-lead">Выберите турнир, где вы являетесь создателем.</p>
                <div className="form-grid">
                  <input
                    className="form-input"
                    type="number"
                    placeholder="ID турнира"
                    value={deputyForm.tournamentId}
                    onChange={(e) => setDeputyForm((prev) => ({ ...prev, tournamentId: e.target.value }))}
                  />
                  <input
                    className="form-input"
                    type="number"
                    placeholder="ID заместителя"
                    value={deputyForm.deputyUserId}
                    onChange={(e) => setDeputyForm((prev) => ({ ...prev, deputyUserId: e.target.value }))}
                  />
                  <button className="menu-btn" type="button" onClick={() => void submitDeputy()} disabled={!canSubmit}>
                    Назначить заместителя
                  </button>
                </div>
              </article>
            ) : null}
          </>
        ) : null}

        {activeTab === "my" ? (
          <article className="row-card">
            <div className="card-title">Мои активные матчи</div>
            {matches.length === 0 ? <p className="meta">Нет активных турнирных матчей</p> : null}
            {matches.map((m) => (
              <div key={m.match_id} className="meta">
                #{m.match_id} | турнир #{m.tournament_id} | {m.round_name} | {(m.scheduled_at ?? "время уточняется").replace("T", " ")}
              </div>
            ))}
          </article>
        ) : null}

        {activeTab === "requests" ? (
          <>
            <article className="row-card">
              <div className="card-title">Создать заявку на замену</div>
              <p className="meta section-lead">Только для игроков указанного матча.</p>
              <div className="form-grid">
                <input
                  className="form-input"
                  type="number"
                  placeholder="ID матча"
                  value={replacementForm.matchId}
                  onChange={(e) => setReplacementForm((prev) => ({ ...prev, matchId: e.target.value }))}
                />
                <input
                  className="form-input"
                  type="number"
                  placeholder="ID выбывающего игрока"
                  value={replacementForm.outUserId}
                  onChange={(e) => setReplacementForm((prev) => ({ ...prev, outUserId: e.target.value }))}
                />
                <input
                  className="form-input"
                  type="number"
                  placeholder="ID входящего игрока"
                  value={replacementForm.inUserId}
                  onChange={(e) => setReplacementForm((prev) => ({ ...prev, inUserId: e.target.value }))}
                />
                <textarea
                  className="form-input form-textarea"
                  placeholder="причина"
                  value={replacementForm.reason}
                  onChange={(e) => setReplacementForm((prev) => ({ ...prev, reason: e.target.value }))}
                />
                <button className="menu-btn" type="button" onClick={() => void submitReplacement()} disabled={!canSubmit}>
                  Отправить заявку на замену
                </button>
              </div>
            </article>

            <article className="row-card">
              <div className="card-title">Создать заявку на проверку ника</div>
              <p className="meta section-lead">Ник проверяется судьёй перед применением.</p>
              <div className="form-grid">
                <input
                  className="form-input"
                  type="number"
                  placeholder="ID турнира"
                  value={nickForm.tournamentId}
                  onChange={(e) => setNickForm((prev) => ({ ...prev, tournamentId: e.target.value }))}
                />
                <input
                  className="form-input"
                  type="text"
                  placeholder="новый ник"
                  value={nickForm.nickname}
                  onChange={(e) => setNickForm((prev) => ({ ...prev, nickname: e.target.value }))}
                />
                <button className="menu-btn" type="button" onClick={() => void submitNickCheck()} disabled={!canSubmit}>
                  Отправить заявку на ник
                </button>
              </div>
            </article>

            <article className="row-card">
              <div className="card-title">Мои открытые заявки</div>
              {openRep.length === 0 && openNick.length === 0 ? <p className="meta">Нет открытых заявок</p> : null}
              {openRep.map((r) => (
                <div key={`rep-${r.id}`} className="meta">
                  [замена] #{r.id} турнир #{r.tournament_id} матч #{r.match_id}: {r.out_user_id} -&gt; {r.in_user_id}
                </div>
              ))}
              {openNick.map((r) => (
                <div key={`nick-${r.id}`} className="meta">
                  [ник] #{r.id} турнир #{r.tournament_id}: {r.requested_nickname}
                </div>
              ))}
            </article>
          </>
        ) : null}

        {activeTab === "judge" ? (
          <article className="row-card">
            <div className="card-title">Открытые заявки судьи</div>
            {judgeRep.length === 0 && judgeNick.length === 0 ? <p className="meta">Нет открытых заявок для судьи</p> : null}
            {judgeRep.map((r) => (
              <div key={`jrep-${r.id}`} className="meta judge-card">
                <div>
                  [замена] #{r.id} турнир #{r.tournament_id} матч #{r.match_id}: {r.out_user_id} -&gt; {r.in_user_id}
                </div>
                <div className="menu-grid" style={{ marginTop: 6 }}>
                  <button className="menu-btn" type="button" onClick={() => void judgeApproveReplacement(r.id)} disabled={!canSubmit}>
                    Одобрить
                  </button>
                  <button
                    className="menu-btn"
                    type="button"
                    onClick={() => {
                      setRejectTarget({ kind: "replacement", requestId: r.id });
                      setRejectReason("несоответствие правилам");
                    }}
                    disabled={!canSubmit}
                  >
                    Отклонить
                  </button>
                </div>
              </div>
            ))}
            {judgeNick.map((r) => (
              <div key={`jnick-${r.id}`} className="meta judge-card">
                <div>
                  [ник] #{r.id} турнир #{r.tournament_id}: пользователь {r.user_id} -&gt; {r.requested_nickname}
                </div>
                <div className="menu-grid" style={{ marginTop: 6 }}>
                  <button className="menu-btn" type="button" onClick={() => void judgeApproveNick(r.id)} disabled={!canSubmit}>
                    Одобрить
                  </button>
                  <button
                    className="menu-btn"
                    type="button"
                    onClick={() => {
                      setRejectTarget({ kind: "nickname", requestId: r.id });
                      setRejectReason("нарушение правил ников");
                    }}
                    disabled={!canSubmit}
                  >
                    Отклонить
                  </button>
                </div>
              </div>
            ))}
          </article>
        ) : null}
      </div>

      {rejectTarget ? (
        <div className="modal-overlay" role="dialog" aria-modal="true">
          <div className="modal-card">
            <h3 className="screen-title">Отклонить заявку #{rejectTarget.requestId}</h3>
            <p className="meta">Тип: {rejectTarget.kind === "replacement" ? "замена" : "ник"}</p>
            <textarea
              className="form-input form-textarea"
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder="Причина отклонения"
            />
            <div className="menu-grid">
              <button className="menu-btn" type="button" onClick={() => setRejectTarget(null)} disabled={!canSubmit}>
                Отмена
              </button>
              <button className="menu-btn" type="button" onClick={() => void submitReject()} disabled={!canSubmit}>
                Подтвердить отклонение
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

