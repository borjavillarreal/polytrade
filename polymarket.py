"""Read-only client for Polymarket public data.

VERIFIED LIVE 2026-06-17 against docs.polymarket.com and the live endpoints:

  Gamma Markets API (https://gamma-api.polymarket.com), no auth:
    GET /markets?closed=false&active=true&limit=&offset=&order=volume&ascending=false
      -> JSON array of market objects. Relevant fields (exact names):
         id, question, conditionId, endDate (ISO8601),
         outcomes (JSON-encoded string array, e.g. "[\"Yes\", \"No\"]"),
         outcomePrices (JSON-encoded string array, e.g. "[\"0.58\", \"0.42\"]"),
         volume / volumeNum, liquidity / liquidityNum,
         clobTokenIds (JSON-encoded string array of CLOB token ids),
         active, closed, archived, enableOrderBook, acceptingOrders,
         umaResolutionStatus.
    GET /markets/{id} -> single market object (same shape).
    Resolution: when closed == true, outcomePrices becomes a one-hot-ish vector
      (e.g. ["1","0"] or ["0","1"]); a 50-50 void resolves to ["0.5","0.5"].

  CLOB API (https://clob.polymarket.com), no auth for reads:
    GET /price?token_id=<id>&side=BUY  -> {"price": "0.42"}  (best ask)
    GET /book?token_id=<id>            -> order book summary (bids/asks/...).

This module performs ZERO writes, holds no credentials, and signs nothing.
"""

import json
import time
from typing import Any, Optional

import requests

import config

_session = requests.Session()
_session.headers.update({"User-Agent": config.HTTP_USER_AGENT})


def _get(url: str, params: Optional[dict] = None) -> Any:
    """GET with light retry/backoff. Returns parsed JSON or raises."""
    last_exc: Optional[Exception] = None
    for attempt in range(config.API_MAX_RETRIES):
        try:
            resp = _session.get(url, params=params, timeout=config.HTTP_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:  # ValueError = bad JSON
            last_exc = exc
            sleep = config.API_BACKOFF_BASE_SECONDS * (2 ** attempt)
            time.sleep(sleep)
    raise RuntimeError(f"GET {url} failed after {config.API_MAX_RETRIES} attempts: {last_exc}")


def _parse_json_array(raw: Any) -> list:
    """Gamma returns outcomes/outcomePrices/clobTokenIds as JSON-encoded strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _to_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def normalize_market(m: dict) -> Optional[dict]:
    """Project a raw Gamma market into the fields the harness cares about.

    Defines a single binary target: the FIRST outcome. `target_prob` is the
    market's implied probability for that outcome at fetch time. For a standard
    Yes/No market the first outcome is "Yes"; for others (e.g. team names) we
    still measure P(first outcome resolves true), which is well defined.

    Returns None if the market is unusable (missing prices / token ids).
    """
    outcomes = _parse_json_array(m.get("outcomes"))
    prices = _parse_json_array(m.get("outcomePrices"))
    token_ids = _parse_json_array(m.get("clobTokenIds"))

    if len(outcomes) < 2 or len(prices) < 2:
        return None

    target_outcome = str(outcomes[0])
    target_prob = _to_float(prices[0], default=-1.0)
    if not (0.0 <= target_prob <= 1.0):
        return None

    return {
        "market_id": str(m.get("id")),
        "condition_id": m.get("conditionId"),
        "question": m.get("question", "").strip(),
        "target_outcome": target_outcome,
        "outcomes": outcomes,
        "target_prob": target_prob,
        "volume": _to_float(m.get("volumeNum", m.get("volume"))),
        "liquidity": _to_float(m.get("liquidityNum", m.get("liquidity"))),
        "resolution_date": m.get("endDate"),
        "yes_token_id": str(token_ids[0]) if token_ids else None,
        "active": bool(m.get("active")),
        "closed": bool(m.get("closed")),
        "enable_order_book": bool(m.get("enableOrderBook")),
    }


def iter_active_markets(limit: int = config.GAMMA_PAGE_LIMIT):
    """Yield raw active, unclosed markets ordered by descending volume."""
    for page in range(config.GAMMA_MAX_PAGES):
        offset = page * limit
        batch = _get(
            f"{config.GAMMA_BASE}/markets",
            params={
                "closed": "false",
                "active": "true",
                "archived": "false",
                "limit": limit,
                "offset": offset,
                "order": "volume",
                "ascending": "false",
            },
        )
        if not isinstance(batch, list) or not batch:
            return
        for m in batch:
            yield m
        if len(batch) < limit:
            return


def get_market(market_id: str) -> Optional[dict]:
    """Fetch a single market's current metadata (used at scoring time)."""
    data = _get(f"{config.GAMMA_BASE}/markets/{market_id}")
    if isinstance(data, list):
        data = data[0] if data else None
    return data


def get_clob_price(token_id: str, side: str = "BUY") -> Optional[float]:
    """Current CLOB price for a token (best ask for BUY, best bid for SELL).

    Not used to freeze the decision price (we freeze the Gamma outcomePrice), but
    available as a cross-check on live liquidity.
    """
    if not token_id:
        return None
    data = _get(f"{config.CLOB_BASE}/price", params={"token_id": token_id, "side": side})
    if isinstance(data, dict) and "price" in data:
        return _to_float(data["price"], default=-1.0)
    return None


def resolution_for(market_id: str) -> Optional[float]:
    """Return the resolved probability of the FIRST outcome, or None if unresolved.

    Resolution semantics (verified): a closed market reports outcomePrices as a
    near one-hot vector. We return outcomePrices[0] as a float in {0.0, 0.5, 1.0}
    (0.5 = void / 50-50). Returns None while the market is still open.
    """
    raw = get_market(market_id)
    if not raw:
        return None
    if not bool(raw.get("closed")):
        return None
    prices = _parse_json_array(raw.get("outcomePrices"))
    if len(prices) < 1:
        return None
    return _to_float(prices[0], default=-1.0)
