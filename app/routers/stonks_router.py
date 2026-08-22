from typing import Any

from fastapi import APIRouter

router = APIRouter()

def solve_test_case(test_case: dict[str, Any]) -> list[str]:
    initial_energy = test_case['energy']
    initial_capital = test_case['capital']
    timeline = test_case['timeline']
    
    years_with_data = {2037}
    for y in timeline:
        years_with_data.add(int(y))
    years = sorted(years_with_data)
    
    stock_names = set()
    for y, stocks in timeline.items():
        for s in stocks:
            stock_names.add(s)
    stock_names = sorted(stock_names)
    
    init_avail = []
    for y, stocks in timeline.items():
        for s, info in stocks.items():
            if info['qty'] > 0:
                init_avail.append(((int(y), s), info['qty']))
    init_avail = tuple(sorted(init_avail))
    
    memo = {}
    
    def dfs(year, en, cap, port, avail):
        state = (year, en, cap, port, avail)
        if state in memo:
            return memo[state]
            
        best_cap = cap
        best_actions = []
        
        prices = {}
        if str(year) in timeline:
            for s, info in timeline[str(year)].items():
                prices[s] = info['price']
                
        port_dict = dict(port)
        sellable_stocks = list(port_dict.keys())
        
        sell_combinations = []
        def gen_sells(idx, current_cap, current_port, current_actions):
            if idx == len(sellable_stocks):
                sell_combinations.append((current_cap, current_port, current_actions))
                return
            s = sellable_stocks[idx]
            current_port_dict = dict(current_port)
            qty = current_port_dict.get(s, 0)
            
            gen_sells(idx + 1, current_cap, current_port, current_actions)
            
            if s in prices and qty > 0:
                new_port = dict(current_port)
                del new_port[s]
                new_cap = current_cap + qty * prices[s]
                new_act = list(current_actions)
                new_act.append(f"s-{s}-{qty}")
                gen_sells(idx + 1, new_cap, tuple(sorted(new_port.items())), tuple(new_act))
                
        gen_sells(0, cap, port, ())
        
        avail_dict = dict(avail)
        buyable_stocks = [s for s in stock_names if s in prices and avail_dict.get((year, s), 0) > 0]
        
        all_trades = []
        for s_cap, s_port, s_actions in sell_combinations:
            def gen_buys(idx, current_cap, current_port, current_avail, current_actions):
                if idx == len(buyable_stocks):
                    all_trades.append((current_cap, current_port, current_avail, current_actions))
                    return
                s = buyable_stocks[idx]
                current_avail_dict = dict(current_avail)
                max_q = current_avail_dict.get((year, s), 0)
                price = prices[s]
                max_affordable = current_cap // price
                max_can_buy = min(max_q, max_affordable)
                
                gen_buys(idx + 1, current_cap, current_port, current_avail, current_actions)
                
                if max_can_buy > 0:
                    new_cap = current_cap - max_can_buy * price
                    new_port = dict(current_port)
                    new_port[s] = new_port.get(s, 0) + max_can_buy
                    new_avail = dict(current_avail)
                    new_avail[(year, s)] -= max_can_buy
                    if new_avail[(year, s)] == 0:
                        del new_avail[(year, s)]
                    new_act = list(current_actions)
                    new_act.append(f"b-{s}-{max_can_buy}")
                    gen_buys(idx + 1, new_cap, tuple(sorted(new_port.items())), tuple(sorted(new_avail.items())), tuple(new_act))
                    
            gen_buys(0, s_cap, s_port, avail, s_actions)
            
        for t_cap, t_port, t_avail, t_actions in all_trades:
            if year == 2037 and t_cap > best_cap:
                best_cap = t_cap
                best_actions = list(t_actions)
            
            for next_y in years:
                if next_y != year:
                    cost = abs(next_y - year)
                    if en >= cost:
                        res_cap, res_actions = dfs(next_y, en - cost, t_cap, t_port, t_avail)
                        if res_cap > best_cap:
                            best_cap = res_cap
                            best_actions = list(t_actions) + [f"j-{year}-{next_y}"] + res_actions
                            
        memo[state] = (best_cap, best_actions)
        return memo[state]
        
    _final_cap, final_actions = dfs(2037, initial_energy, initial_capital, (), init_avail)
    return final_actions

@router.post("/stonks", response_model=list[list[str]])
async def stonks(test_cases: list[dict[str, Any]]):
    results = []
    for tc in test_cases:
        results.append(solve_test_case(tc))
    return results
