from datetime import datetime

from pydantic import BaseModel, Field


class AnalyzeMessageRequest(BaseModel):
    message: str = Field(min_length=1)
    source: str = "manual"
    user_id: str | None = None


class ClassificationResult(BaseModel):
    category: str
    confidence: float
    reason: str
    suggested_action: str


class AnalyzeMessageResponse(BaseModel):
    id: int
    created_at: datetime
    source: str
    user_id: str | None
    message: str
    classification: ClassificationResult


class CategoryBreakdown(BaseModel):
    category: str
    count: int
    percentage: float


class TrendPoint(BaseModel):
    date: str
    category: str
    count: int


class InsightsSummaryResponse(BaseModel):
    total_messages: int
    by_category: list[CategoryBreakdown]
    automation_recommendations: list[str]


class DashboardMetrics(BaseModel):
    total_queries_30d: int
    queries_today: int
    queries_last_7d: int
    unique_customers_30d: int
    active_days_30d: int
    avg_queries_per_active_day: float
    avg_daily_queries_7d: float
    avg_classification_confidence: float
    high_confidence_rate_pct: float
    top_issue_type: str
    top_issue_share_pct: float
    repeat_customer_rate_pct: float
    week_over_week_growth_pct: float
    month_over_month_growth_pct: float


class DashboardResponse(BaseModel):
    metrics: DashboardMetrics
    by_category: list[CategoryBreakdown]
    trends: list[TrendPoint]
    automation_recommendations: list[str]
    sync: dict


class WhatsAppConnectResponse(BaseModel):
    session_id: str
    status: str
    qr_code_data_url: str | None = None
    note: str | None = None
