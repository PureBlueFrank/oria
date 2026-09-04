"""Permanent bounded research-agent primitives."""

from oria.agent.graph import ResearchNodes, build_research_graph, campaign_research_spec
from oria.agent.models import CampaignProposal, campaign_proposal_schema
from oria.agent.spec import ResearchSpec
from oria.agent.state import (
    ResearchLimits,
    ResearchRunContext,
    ResearchState,
    initial_research_state,
)

__all__ = [
    "CampaignProposal",
    "ResearchLimits",
    "ResearchNodes",
    "ResearchRunContext",
    "ResearchSpec",
    "ResearchState",
    "build_research_graph",
    "campaign_proposal_schema",
    "campaign_research_spec",
    "initial_research_state",
]
