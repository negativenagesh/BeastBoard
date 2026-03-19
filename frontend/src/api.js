const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

async function parseJson(res) {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed with ${res.status}`);
  }
  return res.json();
}

export async function fetchSummary(days = 30) {
  const res = await fetch(`${API_URL}/api/insights/summary?days=${days}`);
  return parseJson(res);
}

export async function fetchDashboard(days = 30, trendDays = 30, trendGranularity = "day") {
  const res = await fetch(
    `${API_URL}/api/dashboard?days=${days}&trend_days=${trendDays}&trend_granularity=${trendGranularity}`,
  );

  // Backward compatibility for older backend instances that may not expose
  // /api/dashboard yet but still provide summary + trends endpoints.
  if (res.status === 404) {
    const [summaryRes, trendsRes] = await Promise.all([
      fetch(`${API_URL}/api/insights/summary?days=${days}`),
      fetch(`${API_URL}/api/insights/trends?days=${trendDays}&granularity=${trendGranularity}`),
    ]);
    const summary = await parseJson(summaryRes);
    const trends = await parseJson(trendsRes);

    return {
      metrics: {
        total_queries_30d: summary.total_messages || 0,
        queries_today: 0,
        queries_last_7d: 0,
        unique_customers_30d: 0,
        active_days_30d: 0,
        avg_queries_per_active_day: 0,
        avg_daily_queries_7d: 0,
        avg_classification_confidence: 0,
        high_confidence_rate_pct: 0,
        top_issue_type: "n/a",
        top_issue_share_pct: 0,
        repeat_customer_rate_pct: 0,
        week_over_week_growth_pct: 0,
        month_over_month_growth_pct: 0,
      },
      by_category: summary.by_category || [],
      trends: trends.trends || [],
      automation_recommendations: summary.automation_recommendations || [],
      sync: {
        fetched: 0,
        ingested: 0,
        duplicates: 0,
        note: "Using legacy insights endpoints because /api/dashboard is unavailable.",
      },
    };
  }

  return parseJson(res);
}

export async function streamDashboardExplanations(
  days = 30,
  trendDays = 30,
  trendGranularity = "day",
  onChunk,
  signal,
) {
  const res = await fetch(
    `${API_URL}/api/dashboard/explanations/stream?days=${days}&trend_days=${trendDays}&trend_granularity=${trendGranularity}`,
    {
      method: "GET",
      signal,
    },
  );

  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `Request failed with ${res.status}`);
  }

  if (!res.body) {
    throw new Error("Streaming not supported by this browser.");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    onChunk(decoder.decode(value, { stream: true }));
  }

  const tail = decoder.decode();
  if (tail) onChunk(tail);
}

export async function fetchDashboardExplanations(days = 30, trendDays = 30, trendGranularity = "day") {
  const res = await fetch(
    `${API_URL}/api/dashboard/explanations?days=${days}&trend_days=${trendDays}&trend_granularity=${trendGranularity}`,
  );

  // Optional endpoint: older backend versions may not provide it.
  if (res.status === 404) {
    return {
      explanations: {},
      unavailable: true,
      note: "GPT metric narrative endpoint is not available on the current backend instance.",
    };
  }

  return parseJson(res);
}

export async function fetchWhatsAppMessages(limit = 200) {
  const res = await fetch(`${API_URL}/api/whatsapp/messages?limit=${limit}`);
  if (res.status === 404) {
    return {
      messages: [],
      unavailable: true,
      note: "WhatsApp messages endpoint is not available on the current backend instance.",
    };
  }
  return parseJson(res);
}

export async function deleteWhatsAppMessage(messageId) {
  const res = await fetch(`${API_URL}/api/whatsapp/messages/${messageId}`, {
    method: "DELETE",
  });
  return parseJson(res);
}

export async function fetchTrends(days = 14, granularity = "day") {
  const res = await fetch(`${API_URL}/api/insights/trends?days=${days}&granularity=${granularity}`);
  return parseJson(res);
}

export async function analyzeManualMessage(message) {
  const res = await fetch(`${API_URL}/api/messages/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, source: "dashboard" }),
  });
  return parseJson(res);
}

/**
 * Subscribe to real-time dashboard update events via SSE.
 * Returns the EventSource instance (call .close() to disconnect).
 */
export function subscribeDashboardEvents(onUpdate, onError) {
  const es = new EventSource(`${API_URL}/api/dashboard/events`);

  es.addEventListener("dashboard_updated", () => {
    onUpdate();
  });

  es.onerror = (err) => {
    if (onError) onError(err);
  };

  return es;
}
