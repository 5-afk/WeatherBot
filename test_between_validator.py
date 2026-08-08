"""Assert ContractValidator between-bracket fixes."""

from __future__ import annotations

from src.contract_validator import ContractValidator

RULES = (
    "If the highest temperature recorded in Central Park, New York for August 08, 2026 "
    "as reported by the National Weather Service's Climatological Report (Daily), "
    "is between 89-90, then the market resolves to Yes."
)


def main() -> None:
    v = ContractValidator()

    # Case 1: live-style API (floor=89, cap=90) with bad parse (90, 90)
    m = {
        "status": "active",
        "strike_type": "between",
        "floor_strike": 89,
        "cap_strike": 90,
        "rules_primary": RULES,
        "close_time": "2026-08-09T04:59:00Z",
    }
    r = v.validate("KXHIGHNY-26AUG08-B89.5", m, "KNYC", "KNYC", 90.0, 90.0)
    assert r.valid, r.reason
    assert r.confirmed_threshold == 89.0 and r.confirmed_upper_threshold == 90.0, r
    print("PASS live-style:", r.confirmed_threshold, r.confirmed_upper_threshold)

    # Case 2: equal floor/cap API bug — derive cap = floor + 1 from ticker
    m2 = dict(m)
    m2["floor_strike"] = 90.0
    m2["cap_strike"] = 90.0
    r2 = v.validate("KXHIGHNY-26AUG08-B89.5", m2, "KNYC", "KNYC", 90.0, 90.0)
    assert r2.valid, r2.reason
    assert r2.confirmed_threshold == 90.0 and r2.confirmed_upper_threshold == 91.0, r2
    print("PASS equal floor/cap derive:", r2.confirmed_threshold, r2.confirmed_upper_threshold)

    # Case 3: no B-ticker and equal bounds -> fail clearly
    r3 = v.validate("KXHIGHNY-26AUG08-T89", m2, "KNYC", "KNYC", 90.0, 90.0)
    assert not r3.valid and "cannot determine bracket" in r3.reason.lower(), r3
    print("PASS no-B ticker reject:", r3.reason)
    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
