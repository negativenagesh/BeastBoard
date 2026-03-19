import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  deleteWhatsAppMessage,
  fetchDashboard,
  fetchDashboardExplanations,
  fetchWhatsAppMessages,
  subscribeDashboardEvents,
} from "./api";

const COLORS = ["#f97316", "#06b6d4", "#facc15", "#ef4444", "#22c55e", "#0ea5e9", "#78716c"];
const CHART_COLORS = {
  primary: "#f97316",
  secondary: "#06b6d4",
  accent: "#facc15",
  danger: "#ef4444",
  success: "#22c55e",
};
const CATEGORY_VISIBILITY_KEY = "beastboard.visibleCategories";
const TREND_GRANULARITY_OPTIONS = [
  { key: "day", label: "Day" },
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
];

function mergeTrendSeries(raw) {
  const bucket = new Map();

  raw.forEach((item) => {
    if (!item?.date || !item?.category) return;
    if (!bucket.has(item.date)) bucket.set(item.date, { date: item.date });
    const row = bucket.get(item.date);
    row[item.category] = Number(item.count) || 0;
  });

  return Array.from(bucket.values()).sort((a, b) => String(a.date).localeCompare(String(b.date)));
}

function toDailyVolume(rows) {
  return rows.map((row) => {
    const total = Object.entries(row).reduce((sum, [key, value]) => {
      if (key === "date") return sum;
      return sum + (Number(value) || 0);
    }, 0);

    return { date: row.date, total };
  });
}

function normalizeSavedCategories(value) {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => typeof item === "string" && item.trim().length > 0);
}

function readSavedVisibleCategories() {
  try {
    const raw = window.localStorage.getItem(CATEGORY_VISIBILITY_KEY);
    if (!raw) return [];
    return normalizeSavedCategories(JSON.parse(raw));
  } catch {
    return [];
  }
}

function formatCategoryLabel(value) {
  if (!value) return "-";
  return String(value)
    .split("_")
    .map((token) => token.charAt(0).toUpperCase() + token.slice(1))
    .join(" ");
}

function formatCount(value) {
  return Number(value || 0).toLocaleString();
}

function formatPercent(value, digits = 1) {
  return `${Number(value || 0).toFixed(digits)}%`;
}

function snapshotFingerprint(data, trendGranularity) {
  const payload = {
    trendGranularity,
    metrics: data?.metrics || {},
    by_category: data?.by_category || [],
    trends: data?.trends || [],
  };
  return JSON.stringify(payload);
}

function formatMetricValue(metric, metrics) {
  const raw = metrics?.[metric.key];
  if (metric.type === "percent") return formatPercent(raw, 2);
  if (metric.type === "confidence") return formatPercent(Number(raw || 0) * 100, 1);
  if (metric.type === "decimal") return Number(raw || 0).toFixed(2);
  return formatCount(raw);
}

function renderNoData(message) {
  return <p className="empty-state">{message}</p>;
}

export default function App() {
  const [summary, setSummary] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [trendRows, setTrendRows] = useState([]);
  const [syncState, setSyncState] = useState(null);
  const [metricExplanations, setMetricExplanations] = useState({});
  const [messages, setMessages] = useState([]);
  const [messagesWarning, setMessagesWarning] = useState("");
  const [narrativeWarning, setNarrativeWarning] = useState("");
  const [deletingMessageId, setDeletingMessageId] = useState(null);
  const [explanationsLoading, setExplanationsLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [sseConnected, setSseConnected] = useState(false);
  const [trendGranularity, setTrendGranularity] = useState("day");
  const [visibleCategories, setVisibleCategories] = useState([]);
  const lastNarrativeFingerprint = useRef("");

  const dailyVolumeRows = useMemo(() => toDailyVolume(trendRows), [trendRows]);

  const allTrendCategories = useMemo(() => {
    const names = new Set();
    trendRows.forEach((row) => {
      Object.keys(row).forEach((key) => {
        if (key !== "date") names.add(key);
      });
    });
    return Array.from(names).sort();
  }, [trendRows]);

  const activeTrendCategories = useMemo(() => {
    if (!visibleCategories.length) return allTrendCategories.slice(0, 3);
    const filtered = visibleCategories.filter((category) => allTrendCategories.includes(category));
    return filtered.length > 0 ? filtered : allTrendCategories.slice(0, 3);
  }, [visibleCategories, allTrendCategories]);

  const throughputBars = useMemo(
    () => [
      { label: "Today", value: Number(metrics?.queries_today ?? 0) },
      { label: "7d Avg / Day", value: Number(metrics?.avg_daily_queries_7d ?? 0) },
      { label: "7d Total", value: Number(metrics?.queries_last_7d ?? 0) },
      { label: "30d Total", value: Number(metrics?.total_queries_30d ?? 0) },
    ],
    [metrics]
  );

  const growthBars = useMemo(
    () => [
      { label: "WoW", value: Number(metrics?.week_over_week_growth_pct ?? 0) },
      { label: "MoM", value: Number(metrics?.month_over_month_growth_pct ?? 0) },
    ],
    [metrics]
  );

  const qualityRadar = useMemo(
    () => [
      { metric: "High Confidence", score: Number(metrics?.high_confidence_rate_pct ?? 0) },
      { metric: "Repeat Customer", score: Number(metrics?.repeat_customer_rate_pct ?? 0) },
      { metric: "Top Issue Share", score: Number(metrics?.top_issue_share_pct ?? 0) },
      { metric: "Classifier Confidence", score: Number(metrics?.avg_classification_confidence ?? 0) * 100 },
    ],
    [metrics]
  );

  const confidenceSplit = useMemo(() => {
    const high = Math.max(0, Math.min(100, Number(metrics?.high_confidence_rate_pct ?? 0)));
    return [
      { name: "High Confidence", value: high },
      { name: "Needs Review", value: Math.max(0, 100 - high) },
    ];
  }, [metrics]);

  const metricTiles = [
    { label: "Total Queries (30d)", key: "total_queries_30d", type: "count" },
    { label: "Queries Today", key: "queries_today", type: "count" },
    { label: "Queries Last 7d", key: "queries_last_7d", type: "count" },
    { label: "Unique Customers", key: "unique_customers_30d", type: "count" },
    { label: "Active Days", key: "active_days_30d", type: "count" },
    { label: "Avg Queries / Active Day", key: "avg_queries_per_active_day", type: "decimal" },
    { label: "Avg Daily Queries (7d)", key: "avg_daily_queries_7d", type: "decimal" },
    { label: "Avg Classification Confidence", key: "avg_classification_confidence", type: "confidence" },
    { label: "High Confidence Rate", key: "high_confidence_rate_pct", type: "percent" },
    { label: "Top Issue Share", key: "top_issue_share_pct", type: "percent" },
    { label: "Repeat Customer Rate", key: "repeat_customer_rate_pct", type: "percent" },
    { label: "WoW Growth", key: "week_over_week_growth_pct", type: "percent" },
    { label: "MoM Growth", key: "month_over_month_growth_pct", type: "percent" },
  ];

  function defaultCategorySelection(categories, byCategory) {
    if (categories.length <= 3) return categories;
    const topThree = (byCategory || [])
      .map((row) => row.category)
      .filter((category) => categories.includes(category))
      .slice(0, 3);
    if (topThree.length > 0) return topThree;
    return categories.slice(0, 3);
  }

  async function loadMetricNarrative(currentGranularity) {
    setExplanationsLoading(true);
    setNarrativeWarning("");

    try {
      const data = await fetchDashboardExplanations(30, 30, currentGranularity);
      setMetricExplanations(data.explanations || {});
      if (data.unavailable) {
        setNarrativeWarning(
          data.note || "GPT narrative endpoint is unavailable on the backend instance."
        );
      }
    } catch (e) {
      setNarrativeWarning(`Failed to load GPT narrative: ${String(e)}`);
    } finally {
      setExplanationsLoading(false);
    }
  }

  function formatTimestamp(value) {
    if (!value) return "-";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString();
  }

  function toggleCategory(category) {
    setVisibleCategories((prev) => {
      if (prev.includes(category)) {
        if (prev.length === 1) return prev;
        return prev.filter((item) => item !== category);
      }
      return [...prev, category];
    });
  }

  async function handleDeleteMessage(messageId) {
    if (!window.confirm("Delete this message from dashboard history?")) return;
    setDeletingMessageId(messageId);
    try {
      const result = await deleteWhatsAppMessage(messageId);
      if (result.deleted) {
        setMessages((prev) => prev.filter((item) => item.id !== messageId));
        await loadAll();
      }
    } catch (e) {
      setError(`Failed to delete message: ${String(e)}`);
    } finally {
      setDeletingMessageId(null);
    }
  }

  async function loadAll() {
    try {
      setLoading(true);
      setError("");
      setMessagesWarning("");

      const data = await fetchDashboard(30, 30, trendGranularity);
      const mergedTrends = mergeTrendSeries(data.trends || []);

      const messagesData = await fetchWhatsAppMessages(250).catch((err) => ({
        messages: [],
        unavailable: true,
        note: `Failed to load message history: ${String(err)}`,
      }));

      setSummary({
        by_category: data.by_category || [],
        automation_recommendations: data.automation_recommendations || [],
        total_messages: data.metrics?.total_queries_30d || 0,
      });
      setMetrics(data.metrics || null);
      setSyncState(data.sync || null);
      setTrendRows(mergedTrends);
      setMessages(messagesData.messages || []);

      const trendCategories = Array.from(
        new Set((data.trends || []).map((item) => item.category).filter(Boolean))
      ).sort();

      setVisibleCategories((prev) => {
        const live = prev.filter((category) => trendCategories.includes(category));
        if (live.length > 0) return live;

        const saved = readSavedVisibleCategories();
        const savedLive = saved.filter((category) => trendCategories.includes(category));
        if (savedLive.length > 0) return savedLive;

        return defaultCategorySelection(trendCategories, data.by_category || []);
      });

      if (messagesData.unavailable) {
        setMessagesWarning(
          messagesData.note || "Message history endpoint is unavailable on the backend."
        );
      }

      const nextFingerprint = snapshotFingerprint(data, trendGranularity);
      if (lastNarrativeFingerprint.current !== nextFingerprint) {
        await loadMetricNarrative(trendGranularity);
        lastNarrativeFingerprint.current = nextFingerprint;
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadAll();

    const es = subscribeDashboardEvents(
      () => {
        setSseConnected(true);
        loadAll();
      },
      () => {
        setSseConnected(false);
      }
    );

    es.onopen = () => setSseConnected(true);

    return () => {
      es.close();
    };
  }, [trendGranularity]);

  useEffect(() => {
    if (visibleCategories.length === 0) return;
    window.localStorage.setItem(CATEGORY_VISIBILITY_KEY, JSON.stringify(visibleCategories));
  }, [visibleCategories]);

  function renderGraphExplanations(labels) {
    return (
      <div className="graph-explanations">
        {labels.map((label) => (
          <div className="graph-explanation-item" key={label}>
            <h3>{label}</h3>
            <p>{metricExplanations[label] || (explanationsLoading ? "Generating explanation..." : "Waiting for explanation...")}</p>
          </div>
        ))}
      </div>
    );
  }

  return (
    <div className="page-shell">
      <header className="hero">
        <div className="hero-top-row">
          <p className="project-mark">BeastBoard</p>
          <a
            className="github-link"
            href="https://github.com/negativenagesh/BeastBoard"
            target="_blank"
            rel="noreferrer"
            aria-label="Open BeastBoard GitHub repository"
          >
            <svg viewBox="0 0 24 24" role="img" aria-hidden="true">
              <path d="M12 0C5.37 0 0 5.37 0 12c0 5.3 3.44 9.8 8.2 11.38.6.12.82-.26.82-.58v-2.02c-3.34.72-4.03-1.41-4.03-1.41-.54-1.37-1.33-1.73-1.33-1.73-1.08-.74.08-.72.08-.72 1.2.08 1.83 1.22 1.83 1.22 1.06 1.81 2.79 1.29 3.47.99.11-.77.42-1.29.76-1.58-2.67-.3-5.47-1.34-5.47-5.95 0-1.31.47-2.38 1.22-3.22-.12-.3-.53-1.52.12-3.17 0 0 1-.32 3.3 1.22a11.42 11.42 0 0 1 6 0c2.3-1.54 3.3-1.22 3.3-1.22.65 1.65.24 2.87.12 3.17.76.84 1.22 1.91 1.22 3.22 0 4.62-2.8 5.65-5.48 5.95.43.37.81 1.09.81 2.2v3.26c0 .32.22.7.83.58A12.01 12.01 0 0 0 24 12c0-6.63-5.37-12-12-12z" />
            </svg>
            <span>GitHub</span>
          </a>
        </div>
        <h1>Customer Intelligence Command Center</h1>
        <p>
          Dashboard updates in real-time when new WhatsApp messages arrive.
          <span style={{ marginLeft: "8px", fontSize: "0.85em", color: sseConnected ? "#22c55e" : "#ef4444" }}>
            {sseConnected ? "● Live" : "○ Disconnected"}
          </span>
        </p>
      </header>

      {error && <div className="error">{error}</div>}
      {messagesWarning && <div className="error">{messagesWarning}</div>}
      {narrativeWarning && <div className="error">{narrativeWarning}</div>}

      <div className="workspace-grid">
        <div className="dashboard-column">
          <section className="panel sync-panel">
            <h2>Data Source</h2>
            {syncState ? (
              <>
                <p>
                  Last sync: fetched {formatCount(syncState.fetched)}, ingested {formatCount(syncState.ingested)}, duplicates {formatCount(syncState.duplicates)}
                </p>
                {syncState.note && <p>{syncState.note}</p>}
              </>
            ) : (
              renderNoData("No sync data yet.")
            )}
          </section>

          <section className="cards metrics-grid">
            {metricTiles.map((item) => (
              <article className="card" key={item.label}>
                <span>{item.label}</span>
                <strong>{formatMetricValue(item, metrics)}</strong>
              </article>
            ))}
          </section>

          <section className="panel priority-panel">
            <h2>Automation Opportunities</h2>
            {loading && <p>Loading insights...</p>}
            {(summary?.automation_recommendations || []).length === 0 ? (
              renderNoData("No automation recommendations yet. Ingest more WhatsApp messages to generate playbook actions.")
            ) : (
              <ul className="recommendations">
                {(summary?.automation_recommendations || []).map((line, idx) => (
                  <li key={idx}>{line}</li>
                ))}
              </ul>
            )}
          </section>

          <section className="grid-two">
            <article className="panel">
              <h2>Problem Distribution Dashboard</h2>
              {(summary?.by_category || []).length === 0 ? (
                renderNoData("No category distribution available for the selected window.")
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Issue Type</th>
                        <th>Queries</th>
                        <th>% of Queries</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(summary?.by_category || []).map((row) => (
                        <tr key={row.category}>
                          <td>{formatCategoryLabel(row.category)}</td>
                          <td>{formatCount(row.count)}</td>
                          <td>{formatPercent(row.percentage, 2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </article>

            <article className="panel">
              <h2>Category Share</h2>
              <p className="panel-subtitle">Share of total WhatsApp queries in the selected dashboard window.</p>
              {(summary?.by_category || []).length === 0 ? (
                renderNoData("No data to render category share chart.")
              ) : (
                <div className="chart-wrap">
                  <ResponsiveContainer width="100%" height={320}>
                    <PieChart>
                      <Pie data={summary?.by_category || []} dataKey="count" nameKey="category" outerRadius={110}>
                        {(summary?.by_category || []).map((entry, idx) => (
                          <Cell key={`${entry.category}-${idx}`} fill={COLORS[idx % COLORS.length]} />
                        ))}
                      </Pie>
                      <Tooltip formatter={(value) => formatCount(value)} />
                      <Legend formatter={(value) => formatCategoryLabel(value)} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              )}
              {renderGraphExplanations(["Top Issue Share"])}
            </article>
          </section>

          <section className="grid-two">
            <article className="panel">
              <h2>Category Trend Over Time</h2>
              <div className="trend-controls">
                <div className="trend-granularity" role="group" aria-label="Select trend granularity">
                  {TREND_GRANULARITY_OPTIONS.map((option) => (
                    <button
                      key={option.key}
                      type="button"
                      className={`chip-btn ${trendGranularity === option.key ? "active" : ""}`}
                      onClick={() => setTrendGranularity(option.key)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <div className="category-toggles">
                  {allTrendCategories.map((category) => (
                    <label key={category} className="toggle-item">
                      <input
                        type="checkbox"
                        checked={activeTrendCategories.includes(category)}
                        onChange={() => toggleCategory(category)}
                      />
                      <span>{formatCategoryLabel(category)}</span>
                    </label>
                  ))}
                </div>
              </div>
              {trendRows.length === 0 || activeTrendCategories.length === 0 ? (
                renderNoData("No trend data available for this selection.")
              ) : (
                <div className="chart-wrap">
                  <ResponsiveContainer width="100%" height={320}>
                    <LineChart data={trendRows}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#25334a" />
                      <XAxis dataKey="date" stroke="#95a4be" />
                      <YAxis stroke="#95a4be" allowDecimals={false} />
                      <Tooltip formatter={(value) => formatCount(value)} />
                      <Legend formatter={(value) => formatCategoryLabel(value)} />
                      {activeTrendCategories.map((category, idx) => (
                        <Line
                          key={category}
                          type="monotone"
                          dataKey={category}
                          stroke={COLORS[idx % COLORS.length]}
                          strokeWidth={2.5}
                          dot={false}
                          connectNulls
                        />
                      ))}
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}
              {renderGraphExplanations(["Queries Last 7d", "Top Issue Share"])}
            </article>

            <article className="panel">
              <h2>Most Common Customer Problems</h2>
              {(summary?.by_category || []).length === 0 ? (
                renderNoData("No category ranking available for this period.")
              ) : (
                <div className="chart-wrap">
                  <ResponsiveContainer width="100%" height={320}>
                    <BarChart data={summary?.by_category || []}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#25334a" />
                      <XAxis dataKey="category" stroke="#95a4be" tickFormatter={formatCategoryLabel} />
                      <YAxis stroke="#95a4be" />
                      <Tooltip formatter={(value) => formatCount(value)} labelFormatter={formatCategoryLabel} />
                      <Bar dataKey="count" fill="#f97316" radius={[6, 6, 0, 0]} />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              )}
              {renderGraphExplanations(["Top Issue Share"])}
            </article>
          </section>

          <section className="grid-two">
            <article className="panel">
              <h2>Query Volume Trend</h2>
              {dailyVolumeRows.length === 0 ? (
                renderNoData("No volume trend points available.")
              ) : (
                <div className="chart-wrap">
                  <ResponsiveContainer width="100%" height={320}>
                    <LineChart data={dailyVolumeRows}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#25334a" />
                      <XAxis dataKey="date" stroke="#95a4be" />
                      <YAxis stroke="#95a4be" />
                      <Tooltip formatter={(value) => formatCount(value)} />
                      <Legend />
                      <Line type="monotone" dataKey="total" name="Total Queries" stroke={CHART_COLORS.primary} strokeWidth={3} dot={false} />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}
              {renderGraphExplanations(["Total Queries (30d)", "Queries Today", "Queries Last 7d", "Avg Daily Queries (7d)"])}
            </article>

            <article className="panel">
              <h2>Throughput Benchmark</h2>
              <div className="chart-wrap">
                <ResponsiveContainer width="100%" height={320}>
                  <BarChart data={throughputBars}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#25334a" />
                    <XAxis dataKey="label" stroke="#95a4be" />
                    <YAxis stroke="#95a4be" />
                    <Tooltip formatter={(value) => Number(value).toFixed(2)} />
                    <Bar dataKey="value" fill={CHART_COLORS.secondary} radius={[6, 6, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              {renderGraphExplanations(["Queries Today", "Queries Last 7d", "Avg Daily Queries (7d)"])}
            </article>
          </section>

          <section className="grid-two">
            <article className="panel">
              <h2>Quality & Reliability Radar</h2>
              <div className="chart-wrap">
                <ResponsiveContainer width="100%" height={320}>
                  <RadarChart data={qualityRadar}>
                    <PolarGrid stroke="#2a3e5a" />
                    <PolarAngleAxis dataKey="metric" tick={{ fill: "#c6d3e6", fontSize: 12 }} />
                    <PolarRadiusAxis angle={30} domain={[0, 100]} tick={{ fill: "#95a4be" }} />
                    <Radar
                      name="Score"
                      dataKey="score"
                      stroke={CHART_COLORS.accent}
                      fill={CHART_COLORS.accent}
                      fillOpacity={0.35}
                    />
                    <Tooltip formatter={(value) => formatPercent(value, 1)} />
                  </RadarChart>
                </ResponsiveContainer>
              </div>
              {renderGraphExplanations(["High Confidence Rate", "Repeat Customer Rate", "Top Issue Share", "Avg Classification Confidence"])}
            </article>

            <article className="panel">
              <h2>Growth + Confidence Split</h2>
              <div className="chart-wrap split-charts">
                <ResponsiveContainer width="100%" height={150}>
                  <BarChart data={growthBars}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#25334a" />
                    <XAxis dataKey="label" stroke="#95a4be" />
                    <YAxis stroke="#95a4be" />
                    <Tooltip formatter={(value) => formatPercent(value, 2)} />
                    <Bar dataKey="value" fill={CHART_COLORS.success} radius={[6, 6, 0, 0]} />
                  </BarChart>
                </ResponsiveContainer>
                <ResponsiveContainer width="100%" height={160}>
                  <PieChart>
                    <Pie data={confidenceSplit} dataKey="value" nameKey="name" innerRadius={42} outerRadius={64}>
                      <Cell fill={CHART_COLORS.primary} />
                      <Cell fill={CHART_COLORS.danger} />
                    </Pie>
                    <Tooltip formatter={(value) => formatPercent(value, 1)} />
                  </PieChart>
                </ResponsiveContainer>
              </div>
              {renderGraphExplanations(["WoW Growth", "MoM Growth", "High Confidence Rate"])}
            </article>
          </section>
        </div>

        <aside className="admin-column">
          <section className="panel admin-panel">
            <div className="admin-title-row">
              <h2>WhatsApp Admin Inbox</h2>
              <span>{messages.length} messages</span>
            </div>
            <p className="admin-subtitle">Review synced messages, timestamps, and delete noisy entries from analytics.</p>
            <div className="admin-message-list">
              {messages.length === 0 && <p className="admin-empty">No messages available.</p>}
              {messages.map((item) => (
                <article className="admin-message-card" key={item.id}>
                  <div className="admin-message-meta">
                    <span className="admin-tag">{formatCategoryLabel(item.category)}</span>
                    <span className="admin-time">{formatTimestamp(item.created_at)}</span>
                  </div>
                  <p className="admin-message-text">{item.message}</p>
                  <div className="admin-message-footer">
                    <span>Confidence {formatPercent(Number(item.confidence || 0) * 100, 1)}</span>
                    <button
                      type="button"
                      className="danger-btn"
                      aria-label={`Delete message ${item.id}`}
                      disabled={deletingMessageId === item.id}
                      onClick={() => handleDeleteMessage(item.id)}
                    >
                      {deletingMessageId === item.id ? "Deleting..." : "Delete"}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}
