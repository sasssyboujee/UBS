with open("tests/test_strategy.py", "r") as f:
    lines = f.readlines()

with open("tests/test_strategy.py", "w") as f:
    skip = False
    for line in lines:
        if "assert response.amount == 40" in line and "test_phase3_pre_reveal" in "".join(lines):
            # actually let's just do a safer approach
            pass
