"""analyze.py — ask the model for a calibrated probability on each market.

For every fetched market without a prediction, calls the Anthropic API with the
web_search server tool enabled so the model can gather current context, then
freezes a prediction row (model_prob, frozen market_prob, edge, reasoning, cost).

Idempotent: markets already in `predictions` are skipped, and inserts use the
market_id PRIMARY KEY so a race or re-run cannot double-insert.

Critical measurement constraint baked into the prompt: the model must reason only
from information available as of the fetch timestamp, and must ignore any source
that appears to state the final resolution (to avoid leakage on near-resolved or
already-reported events).

Read-only with respect to Polymarket. The only writes are to the local SQLite DB.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import config
import record

# Convenience: if a local .env file exists, load ANTHROPIC_API_KEY from it.
# Optional — a plain exported env var works too. Never commit .env (it's gitignored).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import anthropic
except ImportError:
    sys.exit("The 'anthropic' package is required: pip install -r requirements.txt")

# Conservative upper bound on a single analysis call's cost. Used only to decide
# whether starting another call could breach a budget cap, so the caps are never
# exceeded (we stop BEFORE a call that might push us over).
_EST_CALL_USD = 0.25


SYSTEM_PROMPT = (
    "You are a calibrated probability forecaster for prediction markets. You are "
    "given a market's question, its RESOLUTION RULES, and the current date. "
    "Estimate the true probability that the market resolves YES, strictly per its "
    "rules. The rules are decisive and often contain conditions the title omits: "
    "deadlines, precise definitions of what counts, and fallback outcomes (a market "
    "may resolve 50-50 / void if neither side occurs by a deadline). Your "
    "probability MUST reflect every such condition; when a 50-50 fallback is "
    "likely, the true probability of YES is pulled toward 0.5 even if the headline "
    "event itself is unlikely. Output strict JSON: {probability: float, confidence: "
    "low|med|high, reasoning: string}."
)


def build_user_prompt(row) -> str:
    """row is a markets-table sqlite3.Row."""
    rules = (row["description"] or "").strip()[:3500]
    rules_block = rules if rules else "(No additional rules text was provided.)"
    return (
        f"FETCH TIMESTAMP (treat this as 'now'): {row['fetch_timestamp']}\n"
        f"MARKET QUESTION: {row['question']}\n"
        f"'Yes' here means the outcome resolves to: {row['target_outcome']}\n"
        f"Scheduled resolution date: {row['resolution_date']}\n\n"
        "RESOLUTION RULES — these govern the outcome; read them carefully:\n"
        f'"""\n{rules_block}\n"""\n\n'
        "Estimate the true probability that this market resolves 'Yes' STRICTLY "
        "per the rules above.\n\n"
        "CONSTRAINTS:\n"
        "- Resolve to the RULES, not the headline. If the rules specify a 50-50 / "
        "void / tie fallback when neither side happens by a deadline, weight it "
        "explicitly: your P(Yes) is pulled toward 0.5 to the extent that fallback "
        "is likely. Honor exact definitions of what counts and every deadline.\n"
        "- Reason ONLY from information available as of the FETCH TIMESTAMP above. "
        "Do not use knowledge of events after that moment.\n"
        "- If a search result appears to state or strongly imply the FINAL "
        "resolution of this exact market, IGNORE it and note that you did so. We "
        "measure foresight, not hindsight.\n"
        "- Do NOT anchor to the current market price; form an independent estimate.\n\n"
        "Respond with ONLY the strict JSON object: "
        '{"probability": <float 0..1>, "confidence": "low|med|high", '
        '"reasoning": "<concise rationale; note how the rules/fallback affected it>"}'
    )


def _build_tools() -> list:
    if not config.ENABLE_WEB_SEARCH:
        return []
    return [{
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": config.WEB_SEARCH_MAX_USES,
    }]


def _estimate_cost(usage, web_search_count: int) -> float:
    """Estimate $ cost from token usage + web searches, using config list prices."""
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cost = (
        inp / 1_000_000 * config.PRICE_INPUT_PER_MTOK
        + out / 1_000_000 * config.PRICE_OUTPUT_PER_MTOK
        + cache_write / 1_000_000 * config.PRICE_CACHE_WRITE_PER_MTOK
        + cache_read / 1_000_000 * config.PRICE_CACHE_READ_PER_MTOK
        + web_search_count / 1_000 * config.PRICE_WEB_SEARCH_PER_1K
    )
    return cost


def _extract_text_and_searches(message) -> tuple[str, int]:
    """Concatenate text blocks; count server-side web_search invocations."""
    text_parts = []
    searches = 0
    for block in message.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(block.text)
        elif btype == "server_tool_use" and getattr(block, "name", "") == "web_search":
            searches += 1
    return "\n".join(text_parts), searches


def _parse_json_object(text: str):
    """Pull the JSON object out of the model's final text. Tolerant of prose/fences."""
    if not text:
        return None
    # Try the whole thing first, then the last {...} block.
    candidates = []
    stripped = text.strip()
    candidates.append(stripped)
    matches = re.findall(r"\{[\s\S]*\}", stripped)
    if matches:
        candidates.append(matches[-1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


def _normalize_confidence(raw) -> str:
    val = str(raw or "").strip().lower()
    if val in ("low", "med", "high"):
        return val
    if val in ("medium", "moderate"):
        return "med"
    return "low"


def call_model(client, row):
    """One analysis call with retry/backoff + pause_turn continuation.

    Returns (model_prob, confidence, reasoning, cost_usd) or None on failure.
    """
    tools = _build_tools()
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    user_prompt = build_user_prompt(row)

    for attempt in range(config.API_MAX_RETRIES):
        try:
            messages = [{"role": "user", "content": user_prompt}]
            total_searches = 0
            total_cost = 0.0
            final_message = None

            # Server-tool loop: resume on pause_turn.
            for _ in range(config.MAX_PAUSE_CONTINUATIONS + 1):
                message = client.messages.create(
                    model=config.ANTHROPIC_MODEL,
                    max_tokens=config.ANTHROPIC_MAX_TOKENS,
                    thinking={"type": "adaptive"},
                    output_config={"effort": config.ANTHROPIC_EFFORT},
                    system=system,
                    tools=tools,
                    messages=messages,
                )
                _, searches = _extract_text_and_searches(message)
                total_searches += searches
                total_cost += _estimate_cost(message.usage, searches)
                final_message = message

                if message.stop_reason == "pause_turn":
                    # Append assistant turn and resume (server continues the tool loop).
                    messages.append({"role": "assistant", "content": message.content})
                    continue
                break

            text, _ = _extract_text_and_searches(final_message)
            parsed = _parse_json_object(text)
            if not parsed or "probability" not in parsed:
                raise ValueError(f"could not parse JSON probability from model output: {text[:200]!r}")

            prob = float(parsed["probability"])
            prob = min(1.0, max(0.0, prob))
            confidence = _normalize_confidence(parsed.get("confidence"))
            reasoning = str(parsed.get("reasoning", "")).strip()
            return prob, confidence, reasoning, total_cost

        except (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            wait = config.API_BACKOFF_BASE_SECONDS * (2 ** attempt)
            print(f"    API error ({type(exc).__name__}); retry in {wait:.0f}s")
            time.sleep(wait)
        except (ValueError, KeyError, TypeError) as exc:
            print(f"    parse error: {exc}")
            return None  # not retryable

    print("    giving up after retries")
    return None


def main() -> None:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("Set ANTHROPIC_API_KEY (read-only LLM access; no Polymarket creds needed).")

    client = anthropic.Anthropic()
    conn = record.connect()
    record.init_db(conn)

    pending = record.markets_without_predictions(conn)
    to_analyze = pending[: config.MAX_ANALYZE_PER_RUN]

    # Budget guard: cumulative $ spent across ALL predictions ever (the lifetime
    # cap is the master safety limit), plus a fresh per-cycle cap.
    lifetime_before = record.total_token_cost(conn)

    analyzed = 0
    skipped = 0
    failed = 0
    run_cost = 0.0
    budget_stop = None

    try:
        for row in to_analyze:
            # Belt-and-suspenders idempotency (a concurrent run may have inserted).
            if record.prediction_exists(conn, row["market_id"]):
                skipped += 1
                continue

            # Stop BEFORE any call that could breach a budget cap.
            if run_cost + _EST_CALL_USD > config.MAX_ANALYSIS_USD_PER_CYCLE:
                budget_stop = (f"per-cycle cap ${config.MAX_ANALYSIS_USD_PER_CYCLE:.2f} "
                               f"reached (spent ${run_cost:.2f} this run)")
                break
            if lifetime_before + run_cost + _EST_CALL_USD > config.MAX_LIFETIME_ANALYSIS_USD:
                budget_stop = (f"lifetime cap ${config.MAX_LIFETIME_ANALYSIS_USD:.2f} "
                               f"reached (spent ${lifetime_before + run_cost:.2f} total)")
                break

            print(f"[{analyzed + failed + 1}/{len(to_analyze)}] {row['question'][:70]}")
            result = call_model(client, row)
            if result is None:
                failed += 1
                time.sleep(config.ANALYZE_RATE_LIMIT_SECONDS)
                continue

            model_prob, confidence, reasoning, cost = result
            market_prob = float(row["yes_price"])  # FROZEN at decision time
            edge = model_prob - market_prob
            decision_ts = datetime.now(timezone.utc).isoformat()

            inserted = record.insert_prediction(conn, {
                "market_id": row["market_id"],
                "question": row["question"],
                "target_outcome": row["target_outcome"],
                "model_prob": model_prob,
                "market_prob": market_prob,
                "edge": edge,
                "model_confidence": confidence,
                "model_reasoning": reasoning,
                "model_name": config.ANTHROPIC_MODEL,
                "token_cost_usd": cost,
                "fetch_timestamp": row["fetch_timestamp"],
                "decision_timestamp": decision_ts,
                "resolution_date": row["resolution_date"],
            })
            conn.commit()

            if inserted:
                analyzed += 1
                run_cost += cost
                print(f"    model={model_prob:.2f}  market={market_prob:.2f}  "
                      f"edge={edge:+.2f}  conf={confidence}  ${cost:.4f}")
            else:
                skipped += 1

            time.sleep(config.ANALYZE_RATE_LIMIT_SECONDS)

        open_preds = record.open_prediction_count(conn)
        resolved = len(record.resolved_predictions(conn))
        lifetime_cost = record.total_token_cost(conn)
        remaining = len(record.markets_without_predictions(conn))
    finally:
        conn.close()

    if budget_stop:
        print(f"  [budget] stopped early: {budget_stop}")

    print("=" * 60)
    print("analyze.py summary")
    print("=" * 60)
    print(f"  model                  : {config.ANTHROPIC_MODEL}")
    print(f"  markets analyzed (new) : {analyzed}")
    print(f"  skipped (already done) : {skipped}")
    print(f"  failed                 : {failed}")
    print(f"  awaiting analysis      : {remaining}")
    print(f"  this-run token cost    : ${run_cost:.4f}  (cap ${config.MAX_ANALYSIS_USD_PER_CYCLE:.2f}/cycle)")
    print(f"  lifetime token cost    : ${lifetime_cost:.4f}  (cap ${config.MAX_LIFETIME_ANALYSIS_USD:.2f})")
    print(f"  open predictions       : {open_preds}")
    print(f"  scored so far          : {resolved}")


if __name__ == "__main__":
    main()
