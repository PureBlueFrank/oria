"""Merchant domain contracts and deterministic eligibility policy."""

from oria.domain.eligibility import EligibilityPolicy
from oria.domain.models import CampaignRuleSet, EligibleMerchantSet, Merchant
from oria.domain.services import CampaignRuleService, DomainServiceRegistry, MerchantService

__all__ = [
    "CampaignRuleService",
    "CampaignRuleSet",
    "DomainServiceRegistry",
    "EligibilityPolicy",
    "EligibleMerchantSet",
    "Merchant",
    "MerchantService",
]
