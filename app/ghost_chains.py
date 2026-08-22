"""Stateful scoring engine for the Ghost Chains AML challenge.

The engine maintains a rolling 24-hour directed graph of transactions and
assigns each incoming transaction a risk score in [0, 1] based on how much the
transaction increases the graph's capacity to support recurring flow.

Structural model
----------------
For an incoming edge u -> v we look at the active graph *before* the edge is
added and count, over every pair (s, t) where s can already reach u and v can
already reach t:

* ``new``  — pairs that were not connected before (the edge extends reach).
* ``par``  — pairs that were already connected (the edge adds an alternative
  route, i.e. convergence / a shortened path).
* ``cycle`` — whether v could already reach u, so the edge closes a loop.
* ``multi`` — if the edge closes a loop, how many other nodes already sit on a
  cycle through the destination; two independent return paths are stronger
  than a single return.

The raw signal is mapped to [0, 1] monotonically.  The first edge between two
isolated entities yields 0.0; extensions, convergences, single returns and
multi-loop returns yield increasingly higher scores.

Temporal model
--------------
Only transactions whose ``createdAt`` lies inside the most recent 24 hours are
active.  Expired edges are removed from the graph before scoring, and the
reachability closure is rebuilt when edges expire.  The boundary is inclusive:
an edge exactly 24 hours old is still active.

Idempotency
-----------
``txId`` values are unique.  A repeated ``txId`` with an identical payload
returns the original score without state mutation; a repeated ``txId`` with a
different payload also returns the original score and is ignored, keeping the
graph consistent with the first observation.
"""

from __future__ import annotations

import heapq
import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

LOOKBACK = timedelta(hours=24)

# Score weights for the structural raw signal.
W_PAR = 2.0      # an alternative route between already-connected nodes
W_CYCLE = 10.0   # the edge closes a return path
W_MULTI = 15.0   # per additional node already on a cycle through destination
SATURATION = 10.0  # risk = raw / (raw + SATURATION)


class GhostChainScorer:
    """Incremental, stateful scoring engine for the Ghost Chains challenge."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """Restore a clean initial state equivalent to startup."""
        with self._lock:
            self.seen: dict[str, tuple[float, str]] = {}
            self.adj: dict[str, dict[str, int]] = {}
            self.edge_heap: list[tuple[float, int, str, str, str]] = []
            self.node_ids: dict[str, int] = {}
            self.id_to_node: list[str] = []
            self.reach: dict[str, int] = {}
            self.rev: dict[str, int] = {}
            self._seq = 0
            self.window_end: float | None = None

    def process(self, transactions: list[Any]) -> list[tuple[str, float]]:
        """Score a batch of transactions sequentially; returns (txId, score)."""
        with self._lock:
            results: list[tuple[str, float]] = []
            for tx in transactions:
                results.append(self._process_one(tx))
            return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _payload_signature(tx: Any) -> str:
        data = {
            "txId": tx.txId,
            "fromUserId": tx.fromUserId,
            "toUserId": tx.toUserId,
            "amount": tx.amount,
            "createdAt": tx.createdAt,
            "ipAddress": tx.ipAddress,
            "deviceId": tx.deviceId,
        }
        return json.dumps(data, sort_keys=True, default=str)

    @staticmethod
    def _parse_time(created_at: str) -> float | None:
        try:
            text = created_at.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
        except (ValueError, AttributeError):
            return None

    def _process_one(self, tx: Any) -> tuple[str, float]:
        signature = self._payload_signature(tx)
        existing = self.seen.get(tx.txId)
        if existing is not None:
            # Idempotent by txId: never mutate state for a repeated id.
            return tx.txId, existing[0]

        timestamp = self._parse_time(tx.createdAt)
        if timestamp is None:
            timestamp = self.window_end if self.window_end is not None else 0.0
        if self.window_end is None or timestamp > self.window_end:
            self.window_end = timestamp

        self._expire_before(timestamp - LOOKBACK.total_seconds())

        risk = self._score_edge(tx.fromUserId, tx.toUserId)
        self._apply_edge(tx.fromUserId, tx.toUserId, timestamp, tx.txId)
        self.seen[tx.txId] = (risk, signature)
        return tx.txId, risk

    def _expire_before(self, cutoff: float) -> None:
        """Drop edges with createdAt strictly before the 24h cutoff."""
        removed = False
        while self.edge_heap and self.edge_heap[0][0] < cutoff:
            _, _, u, v, _ = heapq.heappop(self.edge_heap)
            removed = True
            counts = self.adj.get(u)
            if not counts:
                continue
            count = counts.get(v, 0)
            if count <= 1:
                counts.pop(v, None)
                if not counts:
                    self.adj.pop(u, None)
            else:
                counts[v] = count - 1
        if removed:
            self._rebuild_closure()

    def _node_index(self, node: str) -> int:
        idx = self.node_ids.get(node)
        if idx is None:
            idx = len(self.id_to_node)
            self.node_ids[node] = idx
            self.id_to_node.append(node)
            self.reach[node] = 1 << idx
            self.rev[node] = 1 << idx
        return idx

    @staticmethod
    def _iter_bits(bits: int):
        while bits:
            lsb = bits & -bits
            yield lsb.bit_length() - 1
            bits ^= lsb

    def _score_edge(self, u: str, v: str) -> float:
        self._node_index(u)
        self._node_index(v)

        edge_count = self.adj.get(u, {}).get(v, 0)
        if u == v or edge_count > 0:
            # Degenerate or repeated edge: no new structural capacity.
            return 0.0

        u_idx = self.node_ids[u]
        srcs = self.rev[u]
        dsts = self.reach[v]

        new_pairs = 0
        par_pairs = 0
        for s_idx in self._iter_bits(srcs):
            s_name = self.id_to_node[s_idx]
            reach_s = self.reach[s_name]
            new_pairs += (dsts & ~reach_s).bit_count()
            par_pairs += (dsts & reach_s & ~(1 << s_idx)).bit_count()

        cycle = 1 if (self.reach[v] >> u_idx) & 1 else 0
        multi = 0
        if cycle:
            scc = self.rev[v] & self.reach[v]
            multi = scc.bit_count() - 1

        raw = max(0, new_pairs - 1) + W_PAR * par_pairs + W_CYCLE * cycle + W_MULTI * multi
        return raw / (raw + SATURATION)

    def _apply_edge(self, u: str, v: str, timestamp: float, tx_id: str) -> None:
        self._node_index(u)
        self._node_index(v)

        counts = self.adj.setdefault(u, {})
        is_first_direct_edge = counts.get(v, 0) == 0
        counts[v] = counts.get(v, 0) + 1

        self._seq += 1
        heapq.heappush(self.edge_heap, (timestamp, self._seq, u, v, tx_id))

        if not is_first_direct_edge:
            return

        # Incremental transitive-closure update for the new edge u -> v.
        rev_u = self.rev[u]
        reach_v = self.reach[v]
        for s_idx in self._iter_bits(rev_u):
            s_name = self.id_to_node[s_idx]
            self.reach[s_name] |= reach_v
        for t_idx in self._iter_bits(reach_v):
            t_name = self.id_to_node[t_idx]
            self.rev[t_name] |= rev_u

    def _rebuild_closure(self) -> None:
        """Rebuild node ids and transitive closure from the active edge set."""
        nodes: set[str] = set()
        for u, counts in self.adj.items():
            nodes.add(u)
            nodes.update(counts.keys())

        self.node_ids = {}
        self.id_to_node = []
        self.reach = {}
        self.rev = {}
        for name in sorted(nodes):
            idx = len(self.id_to_node)
            self.node_ids[name] = idx
            self.id_to_node.append(name)
            self.reach[name] = 1 << idx
            self.rev[name] = 1 << idx

        for u in self.id_to_node:
            for v, count in self.adj.get(u, {}).items():
                if count <= 0:
                    continue
                rev_u = self.rev[u]
                reach_v = self.reach[v]
                for s_idx in self._iter_bits(rev_u):
                    s_name = self.id_to_node[s_idx]
                    self.reach[s_name] |= reach_v
                for t_idx in self._iter_bits(reach_v):
                    t_name = self.id_to_node[t_idx]
                    self.rev[t_name] |= rev_u


scorer = GhostChainScorer()
