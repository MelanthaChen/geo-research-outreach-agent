from outreach_agent.screening_provider import estimate_cost


def test_screening_cost_estimate_is_transparent():
    config = type("Config", (), {"input_cost_per_million": 1.0, "output_cost_per_million": 2.0})()
    result = estimate_cost(10, input_tokens_per_candidate=100, output_tokens_per_candidate=50, config=config)
    assert result["input_tokens"] == 1000
    assert result["output_tokens"] == 500
    assert result["estimated_cost_usd"] == 0.002
