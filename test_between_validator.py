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

    # Case 1: live-style API (floor=89, cap=90) with bad title parse (90, 90)
    # This is the real production failure: abs(90-89)=1.0 so old >1.0 guard never fired.
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
    print("PASS live-style API + bad parse:", r.confirmed_threshold, r.confirmed_upper_threshold)

    # Case 2: equal floor/cap API bug — derive [89, 90] from B89.5 center
    m2 = dict(m)
    m2["floor_strike"] = 90.0
    m2["cap_strike"] = 90.0
    r2 = v.validate("KXHIGHNY-26AUG08-B89.5", m2, "KNYC", "KNYC", 90.0, 90.0)
    assert r2.valid, r2.reason
    assert r2.confirmed_threshold == 89.0 and r2.confirmed_upper_threshold == 90.0, r2
    print("PASS equal floor/cap derive from B89.5:", r2.confirmed_threshold, r2.confirmed_upper_threshold)

    # Case 3: integer B-center (B90) with equal API strikes → [90, 91]
    m3 = dict(m)
    m3["floor_strike"] = 90.0
    m3["cap_strike"] = 90.0
    m3["rules_primary"] = RULES.replace("89-90", "90-91")
    r3 = v.validate("KXHIGHNY-26AUG08-B90", m3, "KNYC", "KNYC", 90.0, 90.0)
    assert r3.valid, r3.reason
    assert r3.confirmed_threshold == 90.0 and r3.confirmed_upper_threshold == 91.0, r3
    print("PASS equal floor/cap derive from B90:", r3.confirmed_threshold, r3.confirmed_upper_threshold)

    # Case 4: no B-ticker and equal bounds -> fail clearly
    r4 = v.validate("KXHIGHNY-26AUG08-T89", m2, "KNYC", "KNYC", 90.0, 90.0)
    assert not r4.valid and "cannot determine bracket" in r4.reason.lower(), r4
    print("PASS no-B ticker reject:", r4.reason)

    # Case 5: must never emit inverted-bracket reason for B-tickers with equal strikes
    assert "inverted" not in r2.reason.lower()
    print("ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
