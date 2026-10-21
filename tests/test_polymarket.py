"""Polymarket prediction-market vendor: forward-looking filtering, volume
ranking, formatting, graceful degradation, and router integration.

All API access is mocked, so these run without a network connection.
"""
import copy
import unittest
from unittest import mock

import pytest
import requests

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import router
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.vendors import polymarket


def _market(question, prob, *, volume, end_date, closed=False, wk=None):
    return {
        "question": question,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{prob}", "{round(1 - prob, 4)}"]',
        "volumeNum": volume,
        "endDate": end_date,
        "closed": closed,
        "oneWeekPriceChange": wk,
    }


# One event with a mix: a high-volume open market, a closed one, a past-dated
# one, and a lower-volume open one. Far-future / far-past dates keep the test
# independent of the real clock.
_SEARCH = {
    "events": [
        {
            "markets": [
                _market("Open big?", 0.76, volume=5_000_000, end_date="2030-12-31T00:00:00Z", wk=-0.045),
                _market("Resolved already?", 1.0, volume=9_000_000, end_date="2030-12-31T00:00:00Z", closed=True),
                _market("Past event?", 0.5, volume=8_000_000, end_date="2020-01-01T00:00:00Z"),
                _market("Open small?", 0.30, volume=1_000, end_date="2030-06-30T00:00:00Z"),
            ]
        }
    ]
}


@pytest.mark.unit
class PolymarketFilterTests(unittest.TestCase):
    def test_closed_and_past_markets_are_excluded(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertIn("Open big?", out)
        self.assertIn("Open small?", out)
        self.assertNotIn("Resolved already?", out)  # closed
        self.assertNotIn("Past event?", out)         # endDate in the past

    def test_ranked_by_volume(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertLess(out.index("Open big?"), out.index("Open small?"))

    def test_limit_caps_results(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=1)
        self.assertIn("Open big?", out)
        self.assertNotIn("Open small?", out)


@pytest.mark.unit
class PolymarketFormatTests(unittest.TestCase):
    def test_probability_volume_and_weekly_change_render(self):
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertIn("Yes 76%", out)
        self.assertIn("$5,000,000 volume", out)
        self.assertIn("resolves 2030-12-31", out)
        self.assertIn("1-week -4.5pp", out)  # -0.045 -> -4.5pp

    def test_weekly_change_omitted_when_absent(self):
        # "Open small?" has wk=None -> no 1-week clause on its line.
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        small_line = next(ln for ln in out.splitlines() if "Open small?" in ln)
        self.assertNotIn("1-week", small_line)

    def test_no_matches_reports_clearly(self):
        with mock.patch.object(polymarket, "_request", return_value={"events": []}):
            out = polymarket.get_prediction_markets("obscure ticker", limit=6)
        self.assertIn("No open prediction markets", out)


@pytest.mark.unit
class PolymarketResilienceTests(unittest.TestCase):
    def test_network_error_degrades_gracefully(self):
        # An external-service hiccup must not raise into the analyst.
        with mock.patch.object(
            polymarket, "_request", side_effect=requests.RequestException("boom")
        ):
            out = polymarket.get_prediction_markets("Fed rate cut")
        self.assertIn("unavailable", out.lower())
        self.assertIn("Fed rate cut", out)


@pytest.mark.unit
@pytest.mark.parametrize("value", ['"not an array"', "{}", "null", "123"])
def test_json_list_parser_rejects_non_arrays(value):
    assert polymarket._parse_json_list(value) == []


@pytest.mark.unit
@pytest.mark.parametrize("prices", [
    '[null, "0.5"]', '["NaN", "0.5"]', '["Infinity", "0.5"]',
    '["1.5", "-0.5"]', '["0.5"]', '{"0": "0.5"}', '[true, false]',
])
def test_bad_market_does_not_hide_valid_probabilities(prices):
    data = copy.deepcopy(_SEARCH)
    bad = _market("Invalid market?", .5, volume=9_000_000, end_date="2030-12-31T00:00:00Z")
    bad["outcomePrices"] = prices
    data["events"][0]["markets"].insert(0, bad)
    with mock.patch.object(polymarket, "_request", return_value=data):
        out = polymarket.get_prediction_markets("anything", limit=1)
    assert "Open big?" in out
    assert "Invalid market?" not in out


@pytest.mark.unit
def test_date_only_expiry_does_not_break_the_feed():
    data = copy.deepcopy(_SEARCH)
    data["events"][0]["markets"][0]["endDate"] = "2030-12-31"
    with mock.patch.object(polymarket, "_request", return_value=data):
        assert "Open big?" in polymarket.get_prediction_markets("anything")


@pytest.mark.unit
@pytest.mark.parametrize("volume", ["5000000", None, "bad", float("nan"), float("inf")])
def test_malformed_volume_does_not_break_other_markets(volume):
    data = copy.deepcopy(_SEARCH)
    data["events"][0]["markets"][0]["volumeNum"] = volume
    with mock.patch.object(polymarket, "_request", return_value=data):
        out = polymarket.get_prediction_markets("anything", limit=10)
    assert "Open small?" in out and "Open big?" in out
    assert "nan" not in out.lower() and "inf volume" not in out.lower()
    if volume != "5000000":
        assert "volume unavailable" in out


@pytest.mark.unit
@pytest.mark.parametrize("limit", [0, -1, True, "2"])
def test_invalid_limit_is_rejected_before_fetching(limit):
    with (
        mock.patch.object(polymarket, "_request") as request,
        pytest.raises(ValueError, match="limit"),
    ):
        polymarket.get_prediction_markets("anything", limit=limit)
    request.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("data", [None, [], {}, {"events": None}])
def test_unreadable_response_is_not_reported_as_no_matching_markets(data):
    with mock.patch.object(polymarket, "_request", return_value=data):
        assert "unavailable" in polymarket.get_prediction_markets("anything")


@pytest.mark.unit
@pytest.mark.parametrize("expiry", ["not a date", 123, "2020-01-01"])
def test_invalid_or_expired_dates_cannot_hide_valid_markets(expiry):
    data = copy.deepcopy(_SEARCH)
    bad = _market("Invalid expiry?", .5, volume=9_000_000, end_date=expiry)
    data["events"][0]["markets"].insert(0, bad)
    with mock.patch.object(polymarket, "_request", return_value=data):
        out = polymarket.get_prediction_markets("anything", limit=1)
    assert "Open big?" in out and "Invalid expiry?" not in out


@pytest.mark.unit
class PolymarketRoutingTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_category_routes_to_polymarket(self):
        self.assertEqual(
            router.get_category_for_method("get_prediction_markets"),
            "prediction_markets",
        )
        set_config({"data_vendors": {"prediction_markets": "polymarket"}})
        with mock.patch.dict(
            router.VENDOR_METHODS,
            {"get_prediction_markets": {"polymarket": lambda *a, **k: "POLY_OK"}},
            clear=False,
        ):
            out = router.route_to_vendor("get_prediction_markets", "fed", 5)
        self.assertEqual(out, "POLY_OK")


if __name__ == "__main__":
    unittest.main()
