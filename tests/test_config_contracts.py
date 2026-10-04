import pytest

from mica_agent import SessionSelection
from mica_agent.config_contracts import EffectiveSettings, ModelCapabilities
from mica_agent.models import Message, MessageRole
from mica_agent.providers import ProviderRequest
from mica_agent.reasoning import ReasoningDisplayMode, ReasoningEffort
from mica_agent.tui.models import ReasoningDisplayMode as TuiDisplayMode


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "10"])
def test_budgets_reject_non_positive_integer_and_bool(value):
    with pytest.raises((TypeError, ValueError)):
        ModelCapabilities(max_tokens=value)
    with pytest.raises((TypeError, ValueError)):
        ProviderRequest("chat", (Message(MessageRole.USER, "hello"),), max_tokens=value)


def test_mapping_is_copied_and_display_import_identity_is_preserved():
    mapping = {ReasoningEffort.HIGH: "vendor-high"}
    request = ProviderRequest(
        "chat", (Message(MessageRole.USER, "hello"),), thinking_level_map=mapping
    )
    mapping.clear()
    assert request.thinking_level_map[ReasoningEffort.HIGH] == "vendor-high"
    with pytest.raises(TypeError):
        request.thinking_level_map[ReasoningEffort.HIGH] = "other"
    assert TuiDisplayMode is ReasoningDisplayMode
    assert EffectiveSettings().reasoning_display is ReasoningDisplayMode.COLLAPSED
    assert (
        repr(SessionSelection("TEST_ONLY_SECRET", "chat"))
        == "SessionSelection(<redacted>)"
    )
