MAX_DISCOUNT = 0.15  # level 2 cap

def level_2_discount(list_price, pct):
    # level 2 discount is capped at 15% of list
    return list_price * min(pct, MAX_DISCOUNT)
