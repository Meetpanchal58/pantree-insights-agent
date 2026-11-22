"""Every output format sent to the model must satisfy the provider's strict structured-output rules.

The scripted model in the other tests never checks this, which once let a free-form `params: dict` reach production:
the provider rejected every interpreter call ("additionalProperties is required to be supplied and to be false").
"""
import pytest
from openai.lib._pydantic import to_strict_json_schema

from pantree_insights.agents import Analysis
from pantree_insights.interpret import Alternatives, Interpretation, SpecPatch, TemplateCall
from pantree_insights.templates import TEMPLATES


def _problems(node, path="$") -> list[str]:
    out = []
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            if node.get("additionalProperties") is not False:
                out.append(f"{path}: additionalProperties must be false")
            if set(node.get("properties", {})) != set(node.get("required", [])):
                out.append(f"{path}: every property must be required")
            if not node.get("properties") and node.get("type") == "object":
                out.append(f"{path}: free-form object")
        for key, value in node.items():
            out += _problems(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            out += _problems(value, f"{path}[{i}]")
    return out


@pytest.mark.parametrize("model", [Interpretation, Alternatives, Analysis, SpecPatch], ids=lambda m: m.__name__)
def test_output_format_is_valid_for_strict_structured_output(model):
    assert not _problems(to_strict_json_schema(model))


def test_template_call_names_every_template_and_parameter():
    assert set(TemplateCall.model_fields["name"].annotation.__args__) == set(TEMPLATES)
    declared = set(TemplateCall.model_fields) - {"name"}
    assert all(set(t.params.model_fields) <= declared for t in TEMPLATES.values())


def test_template_call_accepts_the_stored_shape():
    call = TemplateCall.model_validate({"name": "event_lift", "params": {"days_before": 14}})
    assert call.params == {"days_before": 14}


def test_a_malformed_alternative_reading_is_dropped_not_fatal():
    """holdout x11 ended in 'invalid analytical structure' in all four attempts of the latest paid run: one bad
    alternative must not discard an answer whose chosen reading already passed every check."""
    from pantree_insights.interpret.models import Alternatives

    good = {"table": "sales", "measures": [{"name": "c", "metric": "cancellation_rate"}],
            "dimensions": ["payment_type"]}
    bad = {"table": "sales", "measures": [{"name": "c", "metric": "cancellation_rate", "agg": "count"}]}
    parsed = Alternatives.model_validate({"readings": [bad, good], "why": ["grain", "split"]})
    assert [r.dimensions for r in parsed.readings] == [["payment_type"]]
