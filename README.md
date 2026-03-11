# Notional Dashboard

## Current App
- Run with `streamlit run app.py`.
- Current implementation is a single-file Streamlit app with IBKR API integration.

## Why We Are Proceeding With a Re-architecture
- We need resilient, near-real-time behavior and granular UI updates (cell-level changes), not full-page reruns.
- Streamlit's rerun execution model makes partial-update UX difficult and can degrade usability under frequent refresh/retry loops.
- UI logic and IB/data logic are currently tightly coupled, which makes debugging, testing, and failure isolation harder.
- IB-side conditions (farm degradation, entitlements, intermittent quote gaps) require explicit diagnostics and retry policy controls that are better handled in a dedicated backend service.

## Next Steps (Memorialized Plan)
1. Stabilize current Streamlit baseline
- Keep existing diagnostics that separate connection failures from quote-level degradation.
- Keep quote retry queue logic, but only run retries on explicit refresh cycles unless auto-refresh is intentionally enabled.
- Continue avoiding cache writes from fallback-only price sources (cost basis/unavailable).

2. Extract backend service (first major milestone)
- Move IB connection/session management and quote/position aggregation into a standalone Python service.
- Expose:
  - snapshot endpoints (current portfolio state),
  - incremental update channel (websocket/pub-sub),
  - health/diagnostic endpoints (connection, farms, quote quality).
- Add service-level reconnection/backoff policy and structured logging.

3. Build real-time UI against backend
- Replace Streamlit UI for primary dashboard with a frontend that supports efficient incremental updates (target: React + AG Grid).
- Use backend deltas to patch only changed rows/cells.
- Keep Streamlit only for ad hoc diagnostics, if still useful.

4. Cutover and hardening
- Validate parity between legacy Streamlit calculations and new backend outputs.
- Add integration tests for quote fallback/retry paths and NPV calculations.
- Define production runbook (startup order, reconnect behavior, error triage).

## Success Criteria
- Dashboard remains interactive while data refreshes.
- Individual fields update without full-table/whole-page disruption.
- Connection failures and quote-data failures are clearly distinguished.
- Quote quality/fallback behavior is observable and auditable.
- Core portfolio metrics and notional calculations are stable across refreshes.
