
from fastapi import APIRouter, Body

from app.models import StonksTestCase

router = APIRouter()

def solve_test_case(test_case: StonksTestCase) -> list[str]:
    start_energy = test_case.energy
    start_capital = test_case.capital
    timeline = test_case.timeline
    
    available_stocks = {}
    for year_str, stocks in timeline.items():
        year = int(year_str)
        for stock_name, stock_info in stocks.items():
            available_stocks[(year, stock_name)] = {
                'price': stock_info.price,
                'qty': stock_info.qty
            }
            
    best_capital = -1
    best_actions = []
    
    visited_capital = {}
    
    def get_sell_combinations(owned, year):
        stocks_can_sell = []
        for s, qty in owned.items():
            if qty > 0 and str(year) in timeline and s in timeline[str(year)]:
                price = timeline[str(year)][s].price
                stocks_can_sell.append((s, qty, price))
                
        import itertools
        results = []
        for r in range(len(stocks_can_sell) + 1):
            for subset in itertools.combinations(stocks_can_sell, r):
                new_owned = dict(owned)
                cap_gain = 0
                actions = []
                for s, qty, price in subset:
                    new_owned[s] = 0
                    cap_gain += qty * price
                    actions.append(f"s-{s}-{qty}")
                results.append((new_owned, cap_gain, actions))
        return results

    def get_buy_combinations(available, capital, year):
        stocks_can_buy = []
        for (y, s), info in available.items():
            if y == year and info['qty'] > 0:
                stocks_can_buy.append((s, info['qty'], info['price']))
                
        results = []
        import itertools
        if not stocks_can_buy:
            return [({}, {}, 0, [])]
            
        for r in range(len(stocks_can_buy) + 1):
            for subset in itertools.permutations(stocks_can_buy, r):
                rem_cap = capital
                spent = 0
                new_avail_sub = {}
                new_owned_sub = {}
                actions = []
                for s, qty, price in subset:
                    buy_qty = min(qty, rem_cap // price)
                    if buy_qty > 0:
                        spent += buy_qty * price
                        rem_cap -= buy_qty * price
                        new_avail_sub[(year, s)] = buy_qty
                        new_owned_sub[s] = new_owned_sub.get(s, 0) + buy_qty
                        actions.append(f"b-{s}-{buy_qty}")
                results.append((new_avail_sub, new_owned_sub, spent, actions))
        
        unique_results = {}
        for r in results:
            act_tuple = tuple(sorted(r[3]))
            if act_tuple not in unique_results:
                unique_results[act_tuple] = r
        return list(unique_results.values())

    def dfs(year, capital, energy, owned, available, path_actions):
        nonlocal best_capital, best_actions
        
        owned = {k: v for k, v in owned.items() if v > 0}
        
        state = (
            year,
            energy,
            tuple(sorted(owned.items())),
            tuple(sorted((k, v['qty']) for k, v in available.items() if v['qty'] > 0))
        )
        if state in visited_capital and visited_capital[state] >= capital:
            return
        visited_capital[state] = capital
        
        if year == 2037 and capital > best_capital:
            best_capital = capital
            best_actions = list(path_actions)
                
        sell_combos = get_sell_combinations(owned, year)
        
        for new_owned, cap_gain, s_actions in sell_combos:
            curr_cap = capital + cap_gain
            buy_combos = get_buy_combinations(available, curr_cap, year)
            
            for avail_sub, own_sub, cap_spent, b_actions in buy_combos:
                final_cap = curr_cap - cap_spent
                final_owned = dict(new_owned)
                for s, q in own_sub.items():
                    final_owned[s] = final_owned.get(s, 0) + q
                    
                final_avail = {k: dict(v) for k, v in available.items()}
                for k, q in avail_sub.items():
                    final_avail[k]['qty'] -= q
                    
                actions = list(path_actions) + s_actions + b_actions
                
                if year == 2037 and final_cap > best_capital:
                    best_capital = final_cap
                    best_actions = list(actions)
                
                for target_year_str in timeline:
                    target_year = int(target_year_str)
                    if target_year != year:
                        cost = abs(target_year - year)
                        if energy >= cost:
                            dfs(target_year, final_cap, energy - cost, final_owned, final_avail, actions + [f"j-{year}-{target_year}"])

    dfs(2037, start_capital, start_energy, {}, available_stocks, [])
    
    return best_actions

@router.post("/stonks")
def stonks_endpoint(test_cases: list[StonksTestCase] = Body(...)):
    results = []
    for test_case in test_cases:
        results.append(solve_test_case(test_case))
    return results
