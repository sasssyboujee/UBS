"""Stateful scoring engine for the Ghost Chains AML challenge.

The engine maintains a rolling 24-hour directed graph of transactions and
assigns each incoming transaction a risk score in [0, 1] based on how much the
transaction increases the graph's capacity to support recurring flow.

Structural model
----------------
For an incoming edge u -> v we look at the active graph *before* the edge is
added.  The engine maintains transitive reachability plus all-pairs shortest
path distances (in edges), so the effect of the edge can be classified
precisely for every pair (s, t) where s can already reach u and v can already
reach t:

* ``new``  — ``t`` was not reachable from ``s`` before: the edge creates a new
  path.  The trivial direct pair (u, v) is subtracted, so the very first edge
  of a graph contributes nothing: a single isolated transfer is boring.
* ``par``  — ``t`` was already reachable from ``s`` and the route through the
  new edge is no longer than the previous shortest path.  These are genuine
  alternative/shortened routes (convergence); strictly longer detours are
  ignored because they add no capacity for recurring flow.
* ``cycle`` — whether the edge itself closes a loop (v already reached u, or
  u == v).  Only a genuinely new edge can close a loop; a repeated edge does
  not change reachability.
* ``multi`` — if the edge closes a loop, how many existing return edges already
  flow back into the destination (two independent return paths are stronger
  than a single return).

Repeated edges (same u -> v again) create no new reachability but add parallel
capacity, so they score through ``par``.  Self-loops are cycles.

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
  suspicious; when several distinct values already flow into u the change is
  ambiguous and weighted down.
* ``drop``    — the edge omits the attribute even though an earlier leg into u
  carried it.  A consistent flow that stops carrying its identity is
  suspicious; when several distinct values already flow into u the absence is
  ambiguous and weighted down.
* ``reuse``   — the value already appears on active edges in other weakly
  connected components (including a component the edge is about to bridge).
  Shared infrastructure across disconnected components is a coordination hint,
  weaker than shift/drop.
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
active.  The window is anchored at the greatest ``createdAt`` observed so far
(event time), so the window never moves backwards when transactions arrive out
of order.  Expired edges are removed from the graph before scoring, and the
reachability closure is rebuilt when edges expire.  The boundary is inclusive:
an edge exactly 24 hours old is still active.  A transaction whose
``createdAt`` is already outside the window when it arrives is scored but not
inserted into the active graph.

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
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

LOOKBACK = timedelta(hours=24)
INF = float("inf")

# Score weights for the structural raw signal.
W_PAR = 2.0      # an alternative route between already-connected nodes
W_CYCLE = 10.0   # the edge closes a return path
W_MULTI = 15.0   # per additional node already on a cycle through destination

# Score weights for the identity raw signal (Phase 2).
W_SHIFT = 3.0    # identity value changes mid-flow (scaled by ambiguity)
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
            self.dist: list[list[float]] = []
            self.in_mask: dict[str, int] = {}
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
            if text.endswith(("Z", "z")):
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

        cutoff = self.window_end - LOOKBACK.total_seconds()
        self._expire_before(cutoff)

        risk = self._score_edge(tx.fromUserId, tx.toUserId, ip, device)
        if timestamp >= cutoff:
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
            self.in_mask[node] = 0
            for row in self.dist:
                row.append(INF)
            self.dist.append([INF] * (idx + 1))
            self.dist[idx][idx] = 0.0
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
        structural = self._structural_raw(u, v, is_new=edge_count == 0)
        identity = self._identity_raw(u, v, ip, device, structural)
        raw = structural + identity
        return raw / (raw + SATURATION)

    def _structural_raw(self, u: str, v: str, *, is_new: bool) -> float:
        u_idx = self.node_ids[u]
        v_idx = self.node_ids[v]
        srcs = self.rev[u]
        dsts = self.reach[v]

        new_pairs = 0
        par_pairs = 0
        if u != v:
            # A self-loop creates no new simple path between distinct entities:
            # it is scored purely as a cycle below.
            for s_idx in self._iter_bits(srcs):
                d_su = self.dist[s_idx][u_idx]
                if d_su == INF:
                    continue
                d_s = self.dist[s_idx]
                for t_idx in self._iter_bits(dsts):
                    d_vt = self.dist[v_idx][t_idx]
                    if d_vt == INF:
                        continue
                    d_new = d_su + 1.0 + d_vt
                    d_old = d_s[t_idx]
                    if d_old == INF:
                        new_pairs += 1
                    elif d_new <= d_old:
                        par_pairs += 1

        cycle = 0
        multi = 0
        if is_new and (u == v or (self.reach[v] >> u_idx) & 1):
            cycle = 1
            multi = (self.in_mask.get(v, 0) & self.reach[v]).bit_count()

        return max(0, new_pairs - 1) + W_PAR * par_pairs + W_CYCLE * cycle + W_MULTI * multi

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
        raw = 0.0
        agree = 0.0

        if value is not None:
            # Shared identity across disconnected components: a coordination
            # hint, weighted by the number of distinct components other than
            # the source's own that already carry the value.  The destination
            # component is included when it is still disconnected, because an
            # edge that bridges two components which both already carry the
            # value is itself evidence of coordination.
            edges = edge_counts.get(value)
            if edges:
                other_components: set[str] = set()
                for a, _b in edges:
                    comp_a = self._uf_find(a)
                    if comp_a != comp_u:
                        other_components.add(comp_a)
                raw += W_REUSE * len(other_components)

            # Shift versus agreement with the flow entering u.  Several
            # distinct earlier values make either interpretation ambiguous.
            if prev_values:
                if value in prev_values:
                    agree = 1.0 / len(prev_values)
                else:
                    raw += W_SHIFT / len(prev_values)
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

        u_idx = self.node_ids[u]
        v_idx = self.node_ids[v]

        # Incremental transitive-closure update for the new edge u -> v.
        rev_u = self.rev[u]
        reach_v = self.reach[v]
        for s_idx in self._iter_bits(rev_u):
            s_name = self.id_to_node[s_idx]
            self.reach[s_name] |= reach_v
        for t_idx in self._iter_bits(reach_v):
            t_name = self.id_to_node[t_idx]
            self.rev[t_name] |= rev_u

        # Incremental all-pairs shortest-path update.  Any new shortest path
        # uses the inserted edge at most once, so a single pass over the
        # sources that reach u and the targets reachable from v is exact.
        for s_idx in self._iter_bits(rev_u):
            d_su = self.dist[s_idx][u_idx]
            if d_su == INF:
                continue
            d_s = self.dist[s_idx]
            for t_idx in self._iter_bits(reach_v):
                d_vt = self.dist[v_idx][t_idx]
                if d_vt == INF:
                    continue
                d_new = d_su + 1.0 + d_vt
                d_s[t_idx] = min(d_s[t_idx], d_new)

        self.in_mask[v] = self.in_mask.get(v, 0) | (1 << u_idx)

    def _rebuild_closure(self) -> None:
        """Rebuild node ids, transitive closure, distances and weak components."""
        nodes: set[str] = set()
        for u, counts in self.adj.items():
            nodes.add(u)
            nodes.update(counts.keys())

        self.node_ids = {}
        self.id_to_node = []
        self.reach = {}
        self.rev = {}
        self.in_mask = {}
        for name in sorted(nodes):
            idx = len(self.id_to_node)
            self.node_ids[name] = idx
            self.id_to_node.append(name)
            self.reach[name] = 1 << idx
            self.rev[name] = 1 << idx
            self.in_mask[name] = 0

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

        self._rebuild_distances()
        self._rebuild_in_mask()
        self._rebuild_components()

    def _rebuild_distances(self) -> None:
        """Recompute all-pairs shortest distances from the active edge set."""
        n = len(self.id_to_node)
        self.dist = [[INF] * n for _ in range(n)]
        for i in range(n):
            self.dist[i][i] = 0.0

        for u_idx, u_name in enumerate(self.id_to_node):
            row = self.dist[u_idx]
            queue: deque[int] = deque([u_idx])
            while queue:
                x_idx = queue.popleft()
                d_next = row[x_idx] + 1.0
                for v_name, count in self.adj.get(self.id_to_node[x_idx], {}).items():
                    if count <= 0:
                        continue
                    v_idx = self.node_ids[v_name]
                    if row[v_idx] == INF:
                        row[v_idx] = d_next
                        queue.append(v_idx)

    def _rebuild_in_mask(self) -> None:
        """Recompute the in-neighbour bitmask of every node."""
        for u_name, counts in self.adj.items():
            u_idx = self.node_ids[u_name]
            bit = 1 << u_idx
            for v_name, count in counts.items():
                if count > 0:
                    self.in_mask[v_name] = self.in_mask.get(v_name, 0) | bit

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
