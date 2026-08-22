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
* ``fan``  — the destination's existing in-neighbours plus the source's
  existing out-neighbours, so fan-in / fan-out activity is never scored as
  zero.

The raw signal is mapped to [0, 1] monotonically.  The first edge between two
isolated entities yields 0.0; extensions, convergences, single returns and
multi-loop returns yield increasingly higher scores.

Identity model (Phase 2)
------------------------
``ipAddress`` and ``deviceId`` are optional identity dimensions and are scored
independently.  For each dimension the engine tracks, per active edge, which
identity value it carries and which values currently flow into each node.  For
an incoming edge u -> v the identity evidence is:

* ``shift``   — the edge carries a value that differs from the value(s) carried
  by earlier legs into u.  An identity change inside a continuous flow is
  suspicious.
* ``drop``    — the edge omits the attribute even though an earlier leg into u
  carried it.  A consistent flow that stops carrying its identity is
  suspicious; when several distinct values already flow into u the absence is
  ambiguous and weighted down.
* ``reuse``   — the value already appears on active edges in other weakly
  connected components.  Shared infrastructure across disconnected components
  is a coordination hint, weaker than shift/drop.
* ``agree``   — the value matches an earlier leg into u.  Identity lines up
  with the structural flow and slightly reinforces it, scaled by the
  structural score of the edge.

The identity raw signal is added to the structural raw signal and the sum is
mapped to [0, 1] with the same saturation curve, so scores stay comparable
within a running system and the structural ordering is unchanged when no
identity fields are present.

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
W_FAN = 0.5      # per existing in-neighbour of v / out-neighbour of u

# Score weights for the identity raw signal (Phase 2).
W_SHIFT = 3.0    # identity value changes mid-flow
W_DROP = 2.0     # a consistent flow stops carrying its identity
W_REUSE = 1.0    # per disconnected component that already carries the value
W_AGREE = 1.5    # identity lines up with the structural flow (scaled by it)

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
            self.in_deg: dict[str, int] = {}
            self.out_deg: dict[str, int] = {}
            self.edge_heap: list[tuple[float, int, str, str, str, str | None, str | None]] = []
            self.node_ids: dict[str, int] = {}
            self.id_to_node: list[str] = []
            self.reach: dict[str, int] = {}
            self.rev: dict[str, int] = {}
            self._seq = 0
            self.window_end: float | None = None

            # Identity indexes (Phase 2).  Each edge instance contributes to
            # ``ip_edge_counts`` / ``device_edge_counts`` (identity value ->
            # directed edge -> multiplicity) and to ``incoming_ip`` /
            # ``incoming_device`` (destination node -> identity value ->
            # multiplicity of incoming edges carrying it).
            self.ip_edge_counts: dict[str, dict[tuple[str, str], int]] = {}
            self.incoming_ip: dict[str, dict[str, int]] = {}
            self.device_edge_counts: dict[str, dict[tuple[str, str], int]] = {}
            self.incoming_device: dict[str, dict[str, int]] = {}

            # Union-find over weakly connected components of the active graph.
            self.weak_parent: dict[str, str] = {}
            self.weak_size: dict[str, int] = {}

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

    @staticmethod
    def _norm_identity(value: Any) -> str | None:
        """Normalise an optional identity field; empty values count as absent."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

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

        ip = self._norm_identity(getattr(tx, "ipAddress", None))
        device = self._norm_identity(getattr(tx, "deviceId", None))

        self._expire_before(timestamp - LOOKBACK.total_seconds())

        risk = self._score_edge(tx.fromUserId, tx.toUserId, ip, device)
        self._apply_edge(tx.fromUserId, tx.toUserId, timestamp, tx.txId, ip, device)
        self.seen[tx.txId] = (risk, signature)
        return tx.txId, risk

    def _expire_before(self, cutoff: float) -> None:
        """Drop edges with createdAt strictly before the 24h cutoff."""
        removed = False
        while self.edge_heap and self.edge_heap[0][0] < cutoff:
            _, _, u, v, _, ip, device = heapq.heappop(self.edge_heap)
            removed = True
            self._dec_identity(self.ip_edge_counts, self.incoming_ip, u, v, ip)
            self._dec_identity(self.device_edge_counts, self.incoming_device, u, v, device)

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
            self.out_deg[u] = self.out_deg.get(u, 1) - 1
            self.in_deg[v] = self.in_deg.get(v, 1) - 1
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

    def _score_edge(self, u: str, v: str, ip: str | None, device: str | None) -> float:
        self._node_index(u)
        self._node_index(v)

        edge_count = self.adj.get(u, {}).get(v, 0)
        if u == v or edge_count > 0:
            # Degenerate or repeated edge: no new structural capacity.
            return 0.0

        structural = self._structural_raw(u, v)
        identity = self._identity_raw(u, v, ip, device, structural)
        raw = structural + identity
        return raw / (raw + SATURATION)

    def _structural_raw(self, u: str, v: str) -> float:
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

        u_activity = self.in_deg.get(u, 0) + self.out_deg.get(u, 0)
        v_activity = self.in_deg.get(v, 0) + self.out_deg.get(v, 0)
        isolated = u_activity == 0 and v_activity == 0
        baseline = 1 if isolated else 0
        fan = self.in_deg.get(v, 0) + self.out_deg.get(u, 0)

        return (
            max(0, new_pairs - baseline)
            + W_PAR * par_pairs
            + W_FAN * fan
            + W_CYCLE * cycle
            + W_MULTI * multi
        )

    # ------------------------------------------------------------------
    # Identity signal (Phase 2)
    # ------------------------------------------------------------------
    def _identity_raw(
        self,
        u: str,
        v: str,
        ip: str | None,
        device: str | None,
        structural: float,
    ) -> float:
        """Sum the independent ipAddress and deviceId identity signals."""
        raw = 0.0
        raw += self._identity_dim(u, v, ip, self.ip_edge_counts, self.incoming_ip, structural)
        raw += self._identity_dim(
            u, v, device, self.device_edge_counts, self.incoming_device, structural
        )
        return raw

    def _identity_dim(
        self,
        u: str,
        v: str,
        value: str | None,
        edge_counts: dict[str, dict[tuple[str, str], int]],
        incoming: dict[str, dict[str, int]],
        structural: float,
    ) -> float:
        """Score one identity dimension for the incoming edge u -> v."""
        prev_values = set(incoming.get(u, {}))
        if value is None and not prev_values:
            # No identity on this edge and none on earlier legs: absence is
            # normal and carries no signal.
            return 0.0

        comp_u = self._uf_find(u)
        comp_v = self._uf_find(v)
        raw = 0.0
        agree = 0.0

        if value is not None:
            # Shared identity across disconnected components: a coordination
            # hint, weighted by the number of distinct other components.
            edges = edge_counts.get(value)
            if edges:
                other_components: set[str] = set()
                for a, _b in edges:
                    comp_a = self._uf_find(a)
                    if comp_a != comp_u and comp_a != comp_v:
                        other_components.add(comp_a)
                raw += W_REUSE * len(other_components)

            # Shift versus agreement with the flow entering u.
            if prev_values:
                if value in prev_values:
                    agree = 1.0 / len(prev_values)
                else:
                    raw += W_SHIFT
        else:
            # Missing identity after earlier legs carried it.  A consistent
            # flow that stops carrying its identity is suspicious; several
            # distinct earlier values make the absence ambiguous.
            if prev_values:
                raw += W_DROP / len(prev_values)

        # Identity lines up with structural flow: a modest reinforcement
        # scaled by the structural signal of the edge itself.
        if agree and structural > 0:
            raw += W_AGREE * agree * (structural / (structural + SATURATION))
        return raw

    def _uf_find(self, node: str) -> str:
        parent = self.weak_parent
        if node not in parent:
            parent[node] = node
            self.weak_size[node] = 1
            return node
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != node:
            nxt = parent[node]
            parent[node] = root
            node = nxt
        return root

    def _uf_union(self, a: str, b: str) -> None:
        root_a = self._uf_find(a)
        root_b = self._uf_find(b)
        if root_a == root_b:
            return
        if self.weak_size.get(root_a, 1) < self.weak_size.get(root_b, 1):
            root_a, root_b = root_b, root_a
        self.weak_parent[root_b] = root_a
        self.weak_size[root_a] = self.weak_size.get(root_a, 1) + self.weak_size.get(root_b, 1)

    @staticmethod
    def _inc_identity(
        edge_counts: dict[str, dict[tuple[str, str], int]],
        incoming: dict[str, dict[str, int]],
        u: str,
        v: str,
        value: str | None,
    ) -> None:
        if value is None:
            return
        edges = edge_counts.setdefault(value, {})
        edges[(u, v)] = edges.get((u, v), 0) + 1
        node_values = incoming.setdefault(v, {})
        node_values[value] = node_values.get(value, 0) + 1

    @staticmethod
    def _dec_identity(
        edge_counts: dict[str, dict[tuple[str, str], int]],
        incoming: dict[str, dict[str, int]],
        u: str,
        v: str,
        value: str | None,
    ) -> None:
        if value is None:
            return
        edges = edge_counts.get(value)
        if edges is not None:
            count = edges.get((u, v), 0)
            if count <= 1:
                edges.pop((u, v), None)
                if not edges:
                    edge_counts.pop(value, None)
            else:
                edges[(u, v)] = count - 1
        node_values = incoming.get(v)
        if node_values is not None:
            count = node_values.get(value, 0)
            if count <= 1:
                node_values.pop(value, None)
                if not node_values:
                    incoming.pop(v, None)
            else:
                node_values[value] = count - 1

    def _apply_edge(
        self,
        u: str,
        v: str,
        timestamp: float,
        tx_id: str,
        ip: str | None,
        device: str | None,
    ) -> None:
        self._node_index(u)
        self._node_index(v)

        counts = self.adj.setdefault(u, {})
        is_first_direct_edge = counts.get(v, 0) == 0
        counts[v] = counts.get(v, 0) + 1
        self.out_deg[u] = self.out_deg.get(u, 0) + 1
        self.in_deg[v] = self.in_deg.get(v, 0) + 1

        self._seq += 1
        heapq.heappush(self.edge_heap, (timestamp, self._seq, u, v, tx_id, ip, device))

        self._uf_union(u, v)
        self._inc_identity(self.ip_edge_counts, self.incoming_ip, u, v, ip)
        self._inc_identity(self.device_edge_counts, self.incoming_device, u, v, device)

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
        """Rebuild node ids, transitive closure and weak components."""
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

        self._rebuild_components()

    def _rebuild_components(self) -> None:
        """Rebuild the weak-component union-find from the active edge set."""
        self.weak_parent = {}
        self.weak_size = {}
        for u, counts in self.adj.items():
            self._uf_find(u)
            for v, count in counts.items():
                if count <= 0:
                    continue
                self._uf_find(v)
                self._uf_union(u, v)


scorer = GhostChainScorer()
