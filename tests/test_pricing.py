import os
import unittest

from token_dashboard.pricing import load_pricing, cost_for, format_allocation, format_for_user

PRICING = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "pricing.json"))


class CostTests(unittest.TestCase):
    def setUp(self):
        self.p = load_pricing(PRICING)

    def _u(self, **kw):
        base = {
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_create_5m_tokens": 0, "cache_create_1h_tokens": 0,
        }
        base.update(kw)
        return base

    def test_known_opus_input_cost(self):
        c = cost_for("claude-opus-4-7", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)
        self.assertFalse(c["estimated"])

    def test_known_sonnet_output_cost(self):
        c = cost_for("claude-sonnet-4-6", self._u(output_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)

    def test_unknown_opus_falls_back(self):
        c = cost_for("claude-opus-9-9-experimental", self._u(input_tokens=1_000_000), self.p)
        self.assertAlmostEqual(c["usd"], 15.00, places=4)
        self.assertTrue(c["estimated"])

    def test_unknown_unparseable_returns_none(self):
        c = cost_for("custom-local-model", self._u(input_tokens=9999), self.p)
        self.assertIsNone(c["usd"])

    def test_cache_read_cheaper_than_input(self):
        c_in = cost_for("claude-opus-4-7", self._u(input_tokens=1_000_000), self.p)
        c_cr = cost_for("claude-opus-4-7", self._u(cache_read_tokens=1_000_000), self.p)
        self.assertLess(c_cr["usd"], c_in["usd"])


class PlanFormatTests(unittest.TestCase):
    def setUp(self):
        self.p = load_pricing(PRICING)

    def test_api_plan_shows_api_cost_as_headline(self):
        out = format_for_user(12.34, "api", self.p)
        self.assertEqual(out["display_usd"], 12.34)
        self.assertEqual(out["api_cost_usd"], 12.34)
        self.assertFalse(out["is_subscription"])
        self.assertIsNone(out["display_suffix"])
        self.assertIsNone(out["subtitle"])

    def test_pro_plan_shows_monthly_fee_as_headline(self):
        out = format_for_user(12.34, "pro", self.p)
        self.assertTrue(out["is_subscription"])
        self.assertEqual(out["display_usd"], 20.0)
        self.assertEqual(out["display_suffix"], "/mo")
        self.assertEqual(out["api_cost_usd"], 12.34)
        self.assertEqual(out["monthly_fee"], 20)
        self.assertIn("API-equivalent", out["subtitle"])

    def test_subscription_subtitle_contains_api_cost(self):
        out = format_for_user(3167.83, "max", self.p)
        self.assertIn("3,167.83", out["subtitle"])
        self.assertEqual(out["display_usd"], 100.0)

    def test_unknown_plan_falls_back_to_api(self):
        out = format_for_user(12.34, "nonexistent", self.p)
        self.assertFalse(out["is_subscription"])
        self.assertEqual(out["display_usd"], 12.34)


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.p = load_pricing(PRICING)

    def test_api_plan_passes_through_row_cost(self):
        out = format_allocation(42.00, 100.00, "api", self.p, months_in_range=3)
        self.assertEqual(out["display_usd"], 42.00)
        self.assertFalse(out["is_subscription"])
        self.assertIsNone(out["share_of_plan"])

    def test_subscription_allocates_proportional_share(self):
        # Max at $100/mo × 2 months paid = $200; row is 25% of total API cost.
        out = format_allocation(50.00, 200.00, "max", self.p, months_in_range=2)
        self.assertTrue(out["is_subscription"])
        self.assertAlmostEqual(out["share_of_plan"], 0.25, places=4)
        self.assertAlmostEqual(out["display_usd"], 50.00, places=4)  # $200 × 0.25

    def test_subscription_scales_with_months(self):
        a = format_allocation(50.00, 100.00, "max", self.p, months_in_range=1)
        b = format_allocation(50.00, 100.00, "max", self.p, months_in_range=4)
        # Same share (50% of total API cost), but 4× months paid → 4× display.
        self.assertAlmostEqual(b["display_usd"] / a["display_usd"], 4.0, places=4)

    def test_different_plans_produce_different_numbers(self):
        pro = format_allocation(50.00, 100.00, "pro", self.p, months_in_range=1)
        max_ = format_allocation(50.00, 100.00, "max", self.p, months_in_range=1)
        max20 = format_allocation(50.00, 100.00, "max-20x", self.p, months_in_range=1)
        # Same row (50% of total API cost), but different monthly fees → 10/50/100.
        self.assertAlmostEqual(pro["display_usd"],  10.00, places=4)
        self.assertAlmostEqual(max_["display_usd"], 50.00, places=4)
        self.assertAlmostEqual(max20["display_usd"], 100.00, places=4)

    def test_zero_total_api_cost_no_division_error(self):
        out = format_allocation(0.00, 0.00, "max", self.p, months_in_range=1)
        self.assertEqual(out["display_usd"], 0.0)
        self.assertEqual(out["share_of_plan"], 0.0)


if __name__ == "__main__":
    unittest.main()
