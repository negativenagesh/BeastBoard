from app.db import count_messages, fetch_distribution

def generate_automation_recommendations(days: int = 30) -> list[str]:
    distribution = fetch_distribution(days)
    total = count_messages(days)

    if total == 0:
        return [
            "Start ingesting WhatsApp messages to generate baseline support analytics.",
            "Create FAQ intent flows for order tracking and refunds as the first automation layer.",
        ]

    recommendations: list[str] = []
    for item in distribution[:3]:
        category = item["category"]
        pct = item["percentage"]

        if category == "order_tracking":
            recommendations.append(
                f"{pct}% are order-tracking requests: add an auto-reply bot that fetches live shipment status."
            )
        elif category == "delivery_delays":
            recommendations.append(
                f"{pct}% are delivery delays: trigger proactive delay notifications before customers ask."
            )
        elif category == "refund_requests":
            recommendations.append(
                f"{pct}% are refunds: automate refund eligibility checks and one-click request submission."
            )
        elif category == "payment_failures":
            recommendations.append(
                f"{pct}% are payment failures: auto-diagnose failed payment reason and suggest alternate methods."
            )
        elif category == "subscription_issues":
            recommendations.append(
                f"{pct}% are subscription issues: automate plan-change and renewal troubleshooting flows."
            )
        elif category == "product_complaints":
            recommendations.append(
                f"{pct}% are product complaints: auto-collect images/order IDs and open priority tickets."
            )
        else:
            recommendations.append(
                f"{pct}% are general questions: improve self-serve product knowledge base and AI assistant responses."
            )

    return recommendations
