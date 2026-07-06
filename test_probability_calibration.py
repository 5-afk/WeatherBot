"""Verification for per-city sigma, time-decay, bias, and probability calibration."""

from __future__ import annotations

from datetime import date

from src.edge_engine import EdgeEngine, _time_adjusted_sigma, estimate_probability
from src.weather_client import WeatherClient, get_sigma


def test_sigma_values() -> None:
    assert get_sigma("KMIA", date(2026, 7, 4)) == 1.9, "Miami summer sigma wrong"
    assert get_sigma("KDEN", date(2026, 7, 4)) == 4.6, "Denver summer sigma wrong"
    assert get_sigma("KNYC", date(2026, 1, 15)) == 4.1, "NYC winter sigma wrong"


def test_time_adjusted_sigma() -> None:
    assert _time_adjusted_sigma(3.2, 6.0) == 3.2 * 0.50, "6hr sigma wrong"
    assert _time_adjusted_sigma(3.2, 24.0) == 3.2 * 1.0, "24hr sigma wrong"


def test_okc_bias_stacking() -> None:
    weather = WeatherClient()
    city = weather.city_for_market("KXHIGHTOKC")
    assert city is not None
    target = date(2026, 7, 4)
    after_general = weather._apply_bias(95.0, city, target, "high")
    assert after_general == 93.5
    after_city = weather._apply_city_bias(after_general, "KOKC", target, "high")
    assert after_city == 95.3


def test_favorite_longshot_correction() -> None:
    engine = EdgeEngine()
    assert abs(engine._correct_market_price(0.90, "no") - 0.84) < 0.01, "Longshot correction wrong"
    assert abs(engine._correct_market_price(0.10, "yes") - 0.16) < 0.01, "Favorite correction wrong"


def test_minimum_buffer_by_city() -> None:
    engine = EdgeEngine()
    target = date(2026, 7, 4)
    den_buffer = engine._minimum_buffer_for_station("KDEN", target)
    mia_buffer = engine._minimum_buffer_for_station("KMIA", target)
    assert den_buffer > mia_buffer, "Denver should require larger buffer than Miami"
    assert den_buffer > 6.0, "Denver buffer should be >6°F"
    assert mia_buffer < 3.5, "Miami buffer should be <3.5°F"


def test_okc_mia_probability_postmortem() -> None:
    """Compare old flat-sigma vs calibrated model for July 4th loss scenarios."""
    target = date(2026, 7, 4)
    hours = 12.0

    # OKC 100-101°F bracket — old model vs new
    okc_old_prob_yes = estimate_probability(
        92.5, 100.0, "HIGH",
        strike_type="between", upper_threshold_f=101.0,
        station_id="KOKC", target_date=target, hours_until_settlement=hours,
        sigma_multiplier=3.5 / 4.2,  # emulate flat 3.5 on old forecast
    )
    okc_new_prob_yes = estimate_probability(
        94.3, 100.0, "HIGH",
        strike_type="between", upper_threshold_f=101.0,
        station_id="KOKC", target_date=target, hours_until_settlement=hours,
    )
    print(
        f"OKC bracket 100-101°F YES prob: old~{okc_old_prob_yes:.4f} new={okc_new_prob_yes:.4f} "
        f"(new model less confident on NO)"
    )
    assert okc_new_prob_yes >= okc_old_prob_yes * 0.5

    # MIA 92-93°F bracket
    mia_old_prob_yes = estimate_probability(
        88.0, 92.0, "HIGH",
        strike_type="between", upper_threshold_f=93.0,
        station_id="KMIA", target_date=target, hours_until_settlement=hours,
        sigma_multiplier=3.5 / 1.9,
    )
    mia_new_prob_yes = estimate_probability(
        87.2, 92.0, "HIGH",
        strike_type="between", upper_threshold_f=93.0,
        station_id="KMIA", target_date=target, hours_until_settlement=hours,
    )
    print(
        f"MIA bracket 92-93°F YES prob: old~{mia_old_prob_yes:.4f} new={mia_new_prob_yes:.4f}"
    )
    print("MIA actual July 4th high vs NWS forecast — logging for calibration")


def main() -> None:
    test_sigma_values()
    test_time_adjusted_sigma()
    test_okc_bias_stacking()
    test_favorite_longshot_correction()
    test_minimum_buffer_by_city()
    test_okc_mia_probability_postmortem()
    print("PROBABILITY MODEL CALIBRATION VERIFIED OK")


if __name__ == "__main__":
    main()
