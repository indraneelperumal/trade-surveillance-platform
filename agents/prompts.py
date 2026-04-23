import json

SYSTEM_PROMPT = """You are a compliance analyst at a trading surveillance firm.
You generate structured compliance memos based strictly on the
provided trade data and analytical findings.

ANTI-HALLUCINATION RULES — follow exactly:
- Every claim must reference a specific number from the input data.
- Do not infer intent, motivation, or trader behaviour beyond what the data shows.
- Do not reference regulations, laws, or frameworks not mentioned in the input.
- If data is insufficient to draw a conclusion, state that explicitly in data_gaps.
- Output ONLY valid JSON matching the schema below. No commentary outside the JSON.

OUTPUT SCHEMA (respond with this JSON and nothing else):
{
  "summary": "<one factual sentence describing the trade>",
  "evidence_points": ["<fact 1>", "<fact 2>", "<fact 3>"],
  "rule_violated": "<exact rule name from matched_rules, or NONE>",
  "verdict": "<ESCALATE | MONITOR | DISMISS>",
  "confidence": "<HIGH | MEDIUM | LOW>",
  "recommended_action": "<one concrete action>",
  "data_gaps": "<what data is missing that would improve this assessment>"
}"""


def build_user_prompt(state: dict) -> str:
    raw  = state.get("raw_trade", {})
    th   = state.get("trader_history", {})
    mc   = state.get("market_context", {})
    rm   = state.get("rule_match", {})

    shap_raw = raw.get("top_3_shap_features")
    if shap_raw:
        try:
            shap_features = json.loads(shap_raw) if isinstance(shap_raw, str) else shap_raw
        except (json.JSONDecodeError, TypeError):
            shap_features = shap_raw
    else:
        shap_features = []

    matched_rules = rm.get("matched_rules", [])
    severity      = rm.get("severity", "NONE")

    lines = [
        "=== TRADE IDENTIFIERS ===",
        f"trade_id:   {raw.get('trade_id', 'UNKNOWN')}",
        f"symbol:     {raw.get('symbol', 'UNKNOWN')}",
        f"trader_id:  {raw.get('trader_id', 'UNKNOWN')}",
        f"timestamp:  {raw.get('timestamp', 'UNKNOWN')}",
        f"exchange:   {raw.get('exchange', 'UNKNOWN')}",
        f"side:       {raw.get('side', 'UNKNOWN')}",
        f"price:      {raw.get('price', 'UNKNOWN')}",
        f"volume:     {raw.get('volume', 'UNKNOWN')}",
        "",
        "=== ANOMALY SIGNALS ===",
        f"anomaly_score:  {raw.get('anomaly_score', 'UNKNOWN')}",
        f"anomaly_rank:   {raw.get('anomaly_rank', 'UNKNOWN')} (rank 1 = most anomalous)",
        f"anomaly_type:   {raw.get('anomaly_type', 'UNKNOWN')}",
        f"top_shap_feature: {raw.get('top_shap_feature', 'UNKNOWN')}",
        f"top_3_shap_features: {json.dumps(shap_features)}",
        "",
        "=== KEY FEATURE VALUES ===",
        f"z_score_price:        {raw.get('z_score_price', 'UNKNOWN')}",
        f"z_score_volume:       {raw.get('z_score_volume', 'UNKNOWN')}",
        f"spread:               {raw.get('spread', 'UNKNOWN')}",
        f"relative_spread:      {raw.get('relative_spread', 'UNKNOWN')}",
        f"depth_imbalance:      {raw.get('depth_imbalance', 'UNKNOWN')}",
        f"trader_volume_share:  {raw.get('trader_volume_share', 'UNKNOWN')}",
        f"trader_buy_sell_ratio:{raw.get('trader_buy_sell_ratio', 'UNKNOWN')}",
        f"is_off_hours:         {raw.get('is_off_hours', 'UNKNOWN')}",
        f"is_otc:               {raw.get('is_otc', 'UNKNOWN')}",
        f"return_vs_prev:       {raw.get('return_vs_prev', 'UNKNOWN')}",
        "",
        "=== TRADER HISTORY (last 30 trades) ===",
    ]

    if th:
        lines += [
            f"trade_count:              {th.get('trade_count', 'N/A')}",
            f"avg_price:                {th.get('avg_price', 'N/A')}",
            f"avg_volume:               {th.get('avg_volume', 'N/A')}",
            f"off_hours_rate:           {th.get('off_hours_rate', 'N/A')}",
            f"otc_rate:                 {th.get('otc_rate', 'N/A')}",
            f"buy_sell_ratio:           {th.get('buy_sell_ratio', 'N/A')}",
            f"avg_trader_volume_share:  {th.get('avg_trader_volume_share', 'N/A')}",
        ]
    else:
        lines.append("No trader history available (new or unknown trader).")

    lines += [
        "",
        "=== MARKET CONTEXT (±60 min window) ===",
        f"symbol_trade_count_window:         {mc.get('symbol_trade_count_window', 'N/A')}",
        f"symbol_avg_volume_window:          {mc.get('symbol_avg_volume_window', 'N/A')}",
        f"symbol_avg_price_window:           {mc.get('symbol_avg_price_window', 'N/A')}",
        f"symbol_volume_spike:               {mc.get('symbol_volume_spike', 'N/A')}",
        f"price_deviation_from_window_mean:  {mc.get('price_deviation_from_window_mean', 'N/A')}",
        "",
        "=== REGULATORY SCREEN ===",
        f"matched_rules: {matched_rules if matched_rules else 'NONE'}",
        f"severity:      {severity}",
        "",
        "=== INSTRUCTION ===",
        "Generate a compliance memo using ONLY the data above.",
        "Populate data_gaps with anything missing that would strengthen or weaken the assessment.",
        "Output valid JSON only — no text outside the JSON object.",
    ]

    return "\n".join(lines)
