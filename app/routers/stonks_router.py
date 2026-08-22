import heapq
from typing import Any, Dict, List

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

router = APIRouter()

def solve_stonks_case(test_case: Dict[str, Any]) -> List[str]:
    energy = int(test_case.get("energy", 0))
    capital = int(test_case.get("capital", 0))
    raw_timeline = test_case.get("timeline", {})

    # Parse timeline into structured data
    timeline: Dict[int, Dict[str, Dict[str, int]]] = {}
    for y_str, stocks in raw_timeline.items():
        y = int(y_str)
        timeline[y] = {}
        for s_name, data in stocks.items():
            timeline[y][s_name] = {
                "price": int(data["price"]),
                "qty": int(data["qty"]),
            }

    if 2037 not in timeline:
        timeline[2037] = {}

    all_years = list(timeline.keys())

    # Pre-calculate maximum possible selling price per stock
    max_sell_price: Dict[str, int] = {}
    for y, stocks in timeline.items():
        for s_name, data in stocks.items():
            p = data["price"]
            if s_name not in max_sell_price or p > max_sell_price[s_name]:
                max_sell_price[s_name] = p

    # Build initial stock availability tuple
    initial_avail = []
    for y, stocks in timeline.items():
        for s_name, data in stocks.items():
            if data["qty"] > 0:
                initial_avail.append(((y, s_name), data["qty"]))
    initial_avail_tuple = tuple(sorted(initial_avail))

    # Priority Queue for State-Space Search:
    # (-cash, energy_left, current_year, inventory_tuple, available_tuple, actions_tuple)
    pq = [(-capital, energy, 2037, (), initial_avail_tuple, ())]

    visited: Dict[tuple, int] = {}
    best_cash = capital
    best_actions: List[str] = []

    while pq:
        neg_cash, energy_left, curr_year, inv_tuple, avail_tuple, actions = (
            heapq.heappop(pq)
        )
        cash = -neg_cash

        # Check if we can return to 2037 and liquidate
        dist_to_2037 = abs(curr_year - 2037)
        if energy_left >= dist_to_2037:
            end_actions = list(actions)
            if curr_year != 2037:
                end_actions.append(f"j-{curr_year}-2037")

            final_cash = cash
            inv_dict = dict(inv_tuple)
            for s_name, q in inv_dict.items():
                if (
                    s_name in timeline[2037]
                    and timeline[2037][s_name]["price"] > 0
                ):
                    sell_p = timeline[2037][s_name]["price"]
                    final_cash += q * sell_p
                    end_actions.append(f"s-{s_name}-{q}")

            if final_cash > best_cash:
                best_cash = final_cash
                best_actions = end_actions

        # Prune state if we've reached this configuration with equal or better cash
        state_key = (curr_year, inv_tuple, avail_tuple, energy_left)
        if state_key in visited and visited[state_key] >= cash:
            continue
        visited[state_key] = cash

        inv_dict = dict(inv_tuple)
        avail_dict = dict(avail_tuple)

        # 1. Sell stock held in inventory at current year
        if curr_year in timeline:
            for s_name, q in list(inv_dict.items()):
                if s_name in timeline[curr_year]:
                    p = timeline[curr_year][s_name]["price"]
                    new_cash = cash + q * p
                    new_inv = dict(inv_dict)
                    del new_inv[s_name]
                    new_inv_tuple = tuple(sorted(new_inv.items()))
                    new_actions = actions + (f"s-{s_name}-{q}",)

                    heapq.heappush(
                        pq,
                        (
                            -new_cash,
                            energy_left,
                            curr_year,
                            new_inv_tuple,
                            avail_tuple,
                            new_actions,
                        ),
                    )

        # 2. Buy stock available in current year
        if curr_year in timeline:
            for s_name, data in timeline[curr_year].items():
                p = data["price"]
                rem_q = avail_dict.get((curr_year, s_name), 0)
                if rem_q > 0 and cash >= p:
                    # Only buy if there's a higher selling opportunity in another year
                    if max_sell_price.get(s_name, 0) > p:
                        max_buy = min(rem_q, cash // p)
                        if max_buy > 0:
                            new_cash = cash - max_buy * p
                            new_inv = dict(inv_dict)
                            new_inv[s_name] = (
                                new_inv.get(s_name, 0) + max_buy
                            )
                            new_inv_tuple = tuple(sorted(new_inv.items()))

                            new_avail = dict(avail_dict)
                            new_avail[(curr_year, s_name)] = rem_q - max_buy
                            new_avail_tuple = tuple(sorted(new_avail.items()))

                            new_actions = actions + (f"b-{s_name}-{max_buy}",)

                            heapq.heappush(
                                pq,
                                (
                                    -new_cash,
                                    energy_left,
                                    curr_year,
                                    new_inv_tuple,
                                    new_avail_tuple,
                                    new_actions,
                                ),
                            )

        # 3. Time travel to another year
        for y_next in all_years:
            if y_next != curr_year:
                dist = abs(curr_year - y_next)
                if energy_left >= dist:
                    new_energy = energy_left - dist
                    new_actions = actions + (f"j-{curr_year}-{y_next}",)

                    heapq.heappush(
                        pq,
                        (
                            -cash,
                            new_energy,
                            y_next,
                            inv_tuple,
                            avail_tuple,
                            new_actions,
                        ),
                    )

    return best_actions


@router.post("/stonks")
async def stonks_endpoint(request: Request):
    test_cases = await request.json()
    results = [solve_stonks_case(tc) for tc in test_cases]
    return JSONResponse(content=results)