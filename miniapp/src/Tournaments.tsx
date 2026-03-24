import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, apiPost } from "./api";
import { sendTelegramData } from "./telegram";

type TabKey = "create" | "my" | "judge" | "requests";

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
  const [activeTab, setActiveTab] = useState<TabKey>("create");
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const [matches, setMatches] = useState<MatchRow[]>([]);
  const [openRep, setOpenRep] = useState<ReplacementRequest[]>([]);
  const [openNick, setOpenNick] = useState<NickCheckRequest[]>([]);
  const [judgeRep, setJudgeRep] = useState<JudgeReplacementRequest[]>([]);
  const [judgeNick, setJudgeNick] = useState<JudgeNickCheckRequest[]>([]);
  const [syncInfo, setSyncInfo] = useState<{ pending_jobs: number; done_jobs: number } | null>(null);

  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const [replacementForm, setReplacementForm] = useState({
    matchId: "",
    outUserId: "",
    inUserId: "",
    reason: "player disconnected",
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
  const [rejectReason, setRejectReason] = useState("rules mismatch");

  const resetMessages = useCallback(() => {
    setError("");
    setSuccess("");
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    resetMessages();
    try {
      const [m, r, s, j] = await Promise.all([
        apiGet<{ rows: MatchRow[] }>("/api/tournaments/my-matches"),
        apiGet<{ replacement_requests: ReplacementRequest[]; nickname_checks: NickCheckRequest[] }>(
          "/api/tournaments/my-open-requests",
        ),
        apiGet<{ pending_jobs: number; done_jobs: number }>("/api/tournaments/sync-status"),
        apiGet<{ replacement_requests: JudgeReplacementRequest[]; nickname_checks: JudgeNickCheckRequest[] }>(
          "/api/tournaments/judge/open-requests",
        ),
      ]);
      setMatches(m.rows);
      setOpenRep(r.replacement_requests);
      setOpenNick(r.nickname_checks);
      setSyncInfo(s);
      setJudgeRep(j.replacement_requests);
      setJudgeNick(j.nickname_checks);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load tournaments");
    } finally {
      setLoading(false);
    }
  }, [resetMessages]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const canSubmit = useMemo(() => !loading && !submitting, [loading, submitting]);

  const submitReplacement = useCallback(async () => {
    resetMessages();
    const matchId = Number(replacementForm.matchId);
    const outUserId = Number(replacementForm.outUserId);
    const inUserId = Number(replacementForm.inUserId);
    const reason = replacementForm.reason.trim();
    if (!matchId || !outUserId || !inUserId || reason.length < 3) {
      setError("Fill replacement form correctly.");
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
      setSuccess("Replacement request created.");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create replacement request");
    } finally {
      setSubmitting(false);
    }
  }, [replacementForm, refresh, resetMessages]);

  const submitNickCheck = useCallback(async () => {
    resetMessages();
    const tournamentId = Number(nickForm.tournamentId);
    const nickname = nickForm.nickname.trim();
    if (!tournamentId || nickname.length < 3) {
      setError("Fill nickname form correctly.");
      return;
    }
    setSubmitting(true);
    try {
      await apiPost("/api/tournaments/nickname-checks", {
        tournament_id: tournamentId,
        nickname,
      });
      setSuccess("Nickname check request created.");
      setNickForm((prev) => ({ ...prev, nickname: "" }));
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create nickname check");
    } finally {
      setSubmitting(false);
    }
  }, [nickForm, refresh, resetMessages]);

  const submitDeputy = useCallback(async () => {
    resetMessages();
    const tournamentId = Number(deputyForm.tournamentId);
    const deputyUserId = Number(deputyForm.deputyUserId);
    if (!tournamentId || !deputyUserId) {
      setError("Fill deputy form correctly.");
      return;
    }
    setSubmitting(true);
    try {
      await apiPost("/api/tournaments/deputy", {
        tournament_id: tournamentId,
        deputy_user_id: deputyUserId,
      });
      setSuccess("Deputy founder assigned.");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to set deputy founder");
    } finally {
      setSubmitting(false);
    }
  }, [deputyForm, refresh, resetMessages]);

  const submitCreateTournament = useCallback(async () => {
    resetMessages();
    if (!createForm.title.trim()) {
      setError("Tournament title is required.");
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
      setSuccess("Tournament created and sent to moderation (pending).");
      setCreateForm((prev) => ({ ...prev, title: "" }));
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create tournament");
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
        setSuccess(`Replacement request #${requestId} approved.`);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to approve replacement request");
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
        setSuccess(`Nickname request #${requestId} approved.`);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to approve nickname request");
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
      setError("Reject reason is required.");
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
      setSuccess(`${rejectTarget.kind} request #${rejectTarget.requestId} rejected.`);
      setRejectTarget(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to reject request");
    } finally {
      setSubmitting(false);
    }
  }, [rejectReason, rejectTarget, refresh, resetMessages]);

  return (
    <section>
      <h2 className="screen-title">Tournaments</h2>

      <article className="row-card">
        <div className="card-title">Control</div>
        <div className="segment-tabs">
          <button
            type="button"
            className={activeTab === "create" ? "segment-btn active" : "segment-btn"}
            onClick={() => setActiveTab("create")}
          >
            Create
          </button>
          <button
            type="button"
            className={activeTab === "my" ? "segment-btn active" : "segment-btn"}
            onClick={() => setActiveTab("my")}
          >
            My
          </button>
          <button
            type="button"
            className={activeTab === "judge" ? "segment-btn active" : "segment-btn"}
            onClick={() => setActiveTab("judge")}
          >
            Judge
          </button>
          <button
            type="button"
            className={activeTab === "requests" ? "segment-btn active" : "segment-btn"}
            onClick={() => setActiveTab("requests")}
          >
            Requests
          </button>
        </div>
        <div className="menu-grid" style={{ marginTop: 8 }}>
          <button className="menu-btn" type="button" onClick={() => void refresh()} disabled={!canSubmit}>
            Refresh
          </button>
          <button
            className="menu-btn"
            type="button"
            onClick={() => {
              const ok = sendTelegramData({ action: "queue_start", platform: "pc" });
              setSuccess(ok ? "Queue start (PC) sent to bot." : "Open inside Telegram to control queue.");
            }}
            disabled={!canSubmit}
          >
            Start queue (PC)
          </button>
          <button
            className="menu-btn"
            type="button"
            onClick={() => {
              const ok = sendTelegramData({ action: "queue_start", platform: "android" });
              setSuccess(ok ? "Queue start (Android) sent to bot." : "Open inside Telegram to control queue.");
            }}
            disabled={!canSubmit}
          >
            Start queue (Android)
          </button>
          <button
            className="menu-btn"
            type="button"
            onClick={() => {
              const ok = sendTelegramData({ action: "queue_cancel" });
              setSuccess(ok ? "Queue cancel sent to bot." : "Open inside Telegram to control queue.");
            }}
            disabled={!canSubmit}
          >
            Cancel queue
          </button>
        </div>
        {loading ? <p className="meta">Loading...</p> : null}
        {error ? <p className="meta form-error">{error}</p> : null}
        {success ? <p className="meta form-success">{success}</p> : null}
        {syncInfo ? (
          <p className="meta">
            Sync queue: pending {syncInfo.pending_jobs}, done {syncInfo.done_jobs}
          </p>
        ) : null}
      </article>

      <div className="list" style={{ marginTop: 10 }}>
        {activeTab === "create" ? (
          <>
            <article className="row-card">
              <div className="card-title">Create Tournament (Founder)</div>
              <div className="form-grid">
                <input
                  className="form-input"
                  type="text"
                  placeholder="title"
                  value={createForm.title}
                  onChange={(e) => setCreateForm((prev) => ({ ...prev, title: e.target.value }))}
                />
                <div className="menu-grid">
                  <input
                    className="form-input"
                    type="text"
                    placeholder="start_date DD.MM.YYYY"
                    value={createForm.startDate}
                    onChange={(e) => setCreateForm((prev) => ({ ...prev, startDate: e.target.value }))}
                  />
                  <input
                    className="form-input"
                    type="text"
                    placeholder="end_date DD.MM.YYYY"
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
                    <option value="league">league</option>
                    <option value="playoff">playoff</option>
                  </select>
                  <input
                    className="form-input"
                    type="number"
                    placeholder="max_players"
                    value={createForm.maxPlayers}
                    onChange={(e) => setCreateForm((prev) => ({ ...prev, maxPlayers: e.target.value }))}
                  />
                </div>
                <input
                  className="form-input"
                  type="text"
                  placeholder="match_days (0,2,4)"
                  value={createForm.matchDays}
                  onChange={(e) => setCreateForm((prev) => ({ ...prev, matchDays: e.target.value }))}
                />
                <input
                  className="form-input"
                  type="text"
                  placeholder="match_times (18:00,19:00)"
                  value={createForm.matchTimes}
                  onChange={(e) => setCreateForm((prev) => ({ ...prev, matchTimes: e.target.value }))}
                />
                <div className="menu-grid">
                  <input
                    className="form-input"
                    type="number"
                    placeholder="games_per_day"
                    value={createForm.gamesPerDay}
                    onChange={(e) => setCreateForm((prev) => ({ ...prev, gamesPerDay: e.target.value }))}
                  />
                  <input
                    className="form-input"
                    type="number"
                    placeholder="prize_pool_rub"
                    value={createForm.prizePoolRub}
                    onChange={(e) => setCreateForm((prev) => ({ ...prev, prizePoolRub: e.target.value }))}
                  />
                </div>
                <input
                  className="form-input"
                  type="text"
                  placeholder="judges user_ids comma-separated"
                  value={createForm.judges}
                  onChange={(e) => setCreateForm((prev) => ({ ...prev, judges: e.target.value }))}
                />
                <button className="menu-btn" type="button" onClick={() => void submitCreateTournament()} disabled={!canSubmit}>
                  Create tournament
                </button>
              </div>
            </article>

            <article className="row-card">
              <div className="card-title">Set Deputy Founder</div>
              <div className="form-grid">
                <input
                  className="form-input"
                  type="number"
                  placeholder="tournament_id"
                  value={deputyForm.tournamentId}
                  onChange={(e) => setDeputyForm((prev) => ({ ...prev, tournamentId: e.target.value }))}
                />
                <input
                  className="form-input"
                  type="number"
                  placeholder="deputy_user_id"
                  value={deputyForm.deputyUserId}
                  onChange={(e) => setDeputyForm((prev) => ({ ...prev, deputyUserId: e.target.value }))}
                />
                <button className="menu-btn" type="button" onClick={() => void submitDeputy()} disabled={!canSubmit}>
                  Set deputy
                </button>
              </div>
            </article>
          </>
        ) : null}

        {activeTab === "my" ? (
          <article className="row-card">
            <div className="card-title">My Active Matches</div>
            {matches.length === 0 ? <p className="meta">No active tournament matches</p> : null}
            {matches.map((m) => (
              <div key={m.match_id} className="meta">
                #{m.match_id} | tour #{m.tournament_id} | {m.round_name} | {(m.scheduled_at ?? "TBD").replace("T", " ")}
              </div>
            ))}
          </article>
        ) : null}

        {activeTab === "requests" ? (
          <>
            <article className="row-card">
              <div className="card-title">Create Replacement Request</div>
              <div className="form-grid">
                <input
                  className="form-input"
                  type="number"
                  placeholder="match_id"
                  value={replacementForm.matchId}
                  onChange={(e) => setReplacementForm((prev) => ({ ...prev, matchId: e.target.value }))}
                />
                <input
                  className="form-input"
                  type="number"
                  placeholder="out_user_id"
                  value={replacementForm.outUserId}
                  onChange={(e) => setReplacementForm((prev) => ({ ...prev, outUserId: e.target.value }))}
                />
                <input
                  className="form-input"
                  type="number"
                  placeholder="in_user_id"
                  value={replacementForm.inUserId}
                  onChange={(e) => setReplacementForm((prev) => ({ ...prev, inUserId: e.target.value }))}
                />
                <textarea
                  className="form-input form-textarea"
                  placeholder="reason"
                  value={replacementForm.reason}
                  onChange={(e) => setReplacementForm((prev) => ({ ...prev, reason: e.target.value }))}
                />
                <button className="menu-btn" type="button" onClick={() => void submitReplacement()} disabled={!canSubmit}>
                  Submit replacement
                </button>
              </div>
            </article>

            <article className="row-card">
              <div className="card-title">Create Nickname Check</div>
              <div className="form-grid">
                <input
                  className="form-input"
                  type="number"
                  placeholder="tournament_id"
                  value={nickForm.tournamentId}
                  onChange={(e) => setNickForm((prev) => ({ ...prev, tournamentId: e.target.value }))}
                />
                <input
                  className="form-input"
                  type="text"
                  placeholder="new nickname"
                  value={nickForm.nickname}
                  onChange={(e) => setNickForm((prev) => ({ ...prev, nickname: e.target.value }))}
                />
                <button className="menu-btn" type="button" onClick={() => void submitNickCheck()} disabled={!canSubmit}>
                  Submit nickname check
                </button>
              </div>
            </article>

            <article className="row-card">
              <div className="card-title">My Open Requests</div>
              {openRep.length === 0 && openNick.length === 0 ? <p className="meta">No open requests</p> : null}
              {openRep.map((r) => (
                <div key={`rep-${r.id}`} className="meta">
                  [replacement] #{r.id} tour #{r.tournament_id} match #{r.match_id}: {r.out_user_id} -&gt; {r.in_user_id}
                </div>
              ))}
              {openNick.map((r) => (
                <div key={`nick-${r.id}`} className="meta">
                  [nickname] #{r.id} tour #{r.tournament_id}: {r.requested_nickname}
                </div>
              ))}
            </article>
          </>
        ) : null}

        {activeTab === "judge" ? (
          <article className="row-card">
            <div className="card-title">Judge Open Requests</div>
            {judgeRep.length === 0 && judgeNick.length === 0 ? <p className="meta">No open judge requests</p> : null}
            {judgeRep.map((r) => (
              <div key={`jrep-${r.id}`} className="meta judge-card">
                <div>
                  [replacement] #{r.id} tour #{r.tournament_id} match #{r.match_id}: {r.out_user_id} -&gt; {r.in_user_id}
                </div>
                <div className="menu-grid" style={{ marginTop: 6 }}>
                  <button className="menu-btn" type="button" onClick={() => void judgeApproveReplacement(r.id)} disabled={!canSubmit}>
                    Approve
                  </button>
                  <button
                    className="menu-btn"
                    type="button"
                    onClick={() => {
                      setRejectTarget({ kind: "replacement", requestId: r.id });
                      setRejectReason("rules mismatch");
                    }}
                    disabled={!canSubmit}
                  >
                    Reject
                  </button>
                </div>
              </div>
            ))}
            {judgeNick.map((r) => (
              <div key={`jnick-${r.id}`} className="meta judge-card">
                <div>
                  [nickname] #{r.id} tour #{r.tournament_id}: user {r.user_id} -&gt; {r.requested_nickname}
                </div>
                <div className="menu-grid" style={{ marginTop: 6 }}>
                  <button className="menu-btn" type="button" onClick={() => void judgeApproveNick(r.id)} disabled={!canSubmit}>
                    Approve
                  </button>
                  <button
                    className="menu-btn"
                    type="button"
                    onClick={() => {
                      setRejectTarget({ kind: "nickname", requestId: r.id });
                      setRejectReason("nickname policy");
                    }}
                    disabled={!canSubmit}
                  >
                    Reject
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
            <h3 className="screen-title">Reject request #{rejectTarget.requestId}</h3>
            <p className="meta">Type: {rejectTarget.kind}</p>
            <textarea
              className="form-input form-textarea"
              value={rejectReason}
              onChange={(e) => setRejectReason(e.target.value)}
              placeholder="Reject reason"
            />
            <div className="menu-grid">
              <button className="menu-btn" type="button" onClick={() => setRejectTarget(null)} disabled={!canSubmit}>
                Cancel
              </button>
              <button className="menu-btn" type="button" onClick={() => void submitReject()} disabled={!canSubmit}>
                Confirm reject
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

