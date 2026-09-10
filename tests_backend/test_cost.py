import unittest
from decimal import Decimal

from scripts.estimate_cost import estimate, rates


class CostTests(unittest.TestCase):
    def test_all_agents_and_cached_tokens_are_counted_once(self):
        result = estimate("deepseek-v4-flash", 1000000, 100000, 200000, agents=3, turns=2)
        self.assertAlmostEqual(result["model_cost_usd"], 2.9208)
        self.assertEqual(result["total_requests"], 6)
        half = estimate("deepseek-v4-flash", 1000000, 100000, 200000, agents=3, turns=2, off_peak=True)
        self.assertAlmostEqual(half["model_cost_usd"], result["model_cost_usd"] / 2)

    def test_qwen_context_growth_crosses_tiers(self):
        self.assertEqual(rates("qwen3-coder-flash-cn", 32001)[0], Decimal("0.216"))
        result = estimate("qwen3-coder-flash-cn", 32000, 1000, agents=1, turns=2, growth=1000)
        self.assertAlmostEqual(result["model_cost_usd"], (32000*.144+1000*.574+33000*.216+1000*.861)/1000000)

    def test_invalid_inputs_and_infrastructure(self):
        for changes in ({"cached_tokens": 1001}, {"agents": 0}, {"safety_factor": Decimal("NaN")}):
            with self.assertRaises(ValueError):
                estimate("deepseek-v4-pro", 1000, 100, **changes)
        with self.assertRaises(ValueError):
            estimate("qwen3-coder-plus-cn", 1000, 100, off_peak=True)
        self.assertAlmostEqual(estimate("deepseek-v4-flash", 1000, 100, sandbox_hourly=Decimal("0.6"))["sandbox_cost_usd"], .1)


if __name__ == "__main__":
    unittest.main()
