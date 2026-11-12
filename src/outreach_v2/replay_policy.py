"""Durable eligibility rules for deterministic offline response replay."""
from sqlalchemy import and_, exists, false, select

from .db import events
from .semantic_openai import OFFLINE_REPLAY_CONTRACT_ID

LEGACY_EXHAUSTED_OUTCOME = 'KNOWN_RESPONSE_REPLAY_REJECTED_AFTER_CLASSIFIER_REPAIR_V3'
REPLAY_FAILURE_EVENT = 'PERSONALIZATION_OFFLINE_REPLAY_REJECTED'


def replay_allowed_for_contract(candidate_id, outcome_column):
    """Require that this candidate has not failed under the current contract.

    Older V3 outcome rows predate the durable replay event and are treated as
    having exhausted the current V3 contract. A deliberate future contract
    change can reopen them once; a replay failure then records that contract.
    """
    already_failed = exists(select(events.c.id).where(
        events.c.entity_id == candidate_id,
        events.c.event == REPLAY_FAILURE_EVENT,
        events.c.detail.like(f'%"validator_contract":"{OFFLINE_REPLAY_CONTRACT_ID}"%')))
    legacy_exhausted = (outcome_column == LEGACY_EXHAUSTED_OUTCOME
                        if OFFLINE_REPLAY_CONTRACT_ID == 'service-statement-citation-v3'
                        else false())
    return and_(~already_failed, ~legacy_exhausted)
