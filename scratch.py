from app.strategy import _equity_post_multi

print(_equity_post_multi(13, 1, "standard", 5)) # King, community Ace (so we don't have a pair).
print(_equity_post_multi(13, 13, "standard", 5)) # King, community King (we have a pair).
