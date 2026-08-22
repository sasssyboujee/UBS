import heapq
import requests
import tiktoken
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://tool-box-2591eaa24fa3.herokuapp.com"
TOKEN_ENCODING = tiktoken.get_encoding("o200k_base")

# Initialize FastMCP instance
mcp = FastMCP("ShowdownToolServer")


@mcp.tool()
def recall_passages(query: str) -> list[str]:
    """Retrieves relevant passages from the 5 study material documents.
    Enforces a strict total limit of <= 900 o200k_base tokens.
    """
    materials = [f"{BASE_URL}/study-materials/{i}" for i in range(1, 6)]

    candidate_passages = []
    for url in materials:
        try:
            res = requests.get(url, timeout=5)
            if res.status_code == 200:
                paragraphs = [
                    p.strip() for p in res.text.split("\n\n") if p.strip()
                ]
                candidate_passages.extend(paragraphs)
        except Exception:
            continue

    keywords = [k.lower() for k in query.split() if len(k) > 3]
    scored = []
    for passage in candidate_passages:
        score = sum(1 for kw in keywords if kw in passage.lower())
        scored.append((score, passage))

    scored.sort(key=lambda x: x[0], reverse=True)

    selected_passages = []
    total_tokens = 0

    for _, passage in scored:
        tokens = len(TOKEN_ENCODING.encode(passage))
        if total_tokens + tokens <= 880:  # Safety margin under 900 ceiling
            selected_passages.append(passage)
            total_tokens += tokens
        if total_tokens >= 850:
            break

    return selected_passages


@mcp.tool()
def navigate_step(
    map_id: str,
    current_node: str,
    destination_node: str,
    hop_allowance: int | None = None,
) -> str:
    """Calculates the optimal next step on a directed weighted graph.
    Cost = sum(edge_weights) + sum(entry_tolls). Returns ONLY the next node name.
    """
    res = requests.get(
        f"{BASE_URL}/graph", params={"map_id": map_id}, timeout=5
    )
    res.raise_for_status()
    graph_data = res.json()

    adjacency = graph_data.get("adjacency", {})
    tolls = graph_data.get("tolls", {})

    if current_node == destination_node:
        return destination_node

    pq = [(0.0, 0, current_node, [current_node])]
    visited = {}

    while pq:
        cost, hops, curr, path = heapq.heappop(pq)

        if curr == destination_node:
            return path[1]

        if hop_allowance is not None and hops >= hop_allowance:
            continue

        state = (curr, hops)
        if state in visited and visited[state] <= cost:
            continue
        visited[state] = cost

        for neighbor, edge_weight in adjacency.get(curr, {}).items():
            if neighbor in path:  # Avoid cycle revisits
                continue

            step_cost = edge_weight + tolls.get(neighbor, 0.0)
            heapq.heappush(
                pq, (cost + step_cost, hops + 1, neighbor, path + [neighbor])
            )

    raise ValueError(
        f"No valid path from {current_node} to {destination_node}"
    )