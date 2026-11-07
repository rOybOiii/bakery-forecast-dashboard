"""Manager review commands for provenance-driven event reconciliation.

The pipeline owns detection and proposal generation.  This module is the
transactional application boundary used by Streamlit: it reads durable
proposals and records a manager's accept, reject, redirect, or reopen choice.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from gateway_client import active_gateway_scope, application_call

from database_event_families import _engine, refresh_family


DECISION_LABELS = {
    "pending": "Needs confirmation",
    "auto_accepted": "Auto-reconciled",
    "accepted": "Manager-confirmed",
    "rejected": "Rejected",
}


def load_event_reconciliations(site_id: int) -> list[dict[str, Any]]:
    if active_gateway_scope() is not None:
        return application_call("workspace", "events.load_reconciliations", site_id)
    """Return reconciliation proposals for the site's current forecast run."""

    engine = _engine()
    if engine is None:
        return []
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    WITH current_run AS (
                        SELECT a.run_id
                        FROM current_forecast forecast
                        JOIN artifact a ON a.id = forecast.artifact_id
                        WHERE forecast.site_id = :site_id
                        ORDER BY forecast.published_at DESC, a.run_id DESC
                        LIMIT 1
                    )
                    SELECT link.id, link.dispatch_id, link.source_event_id,
                        link.target_event_id, link.relationship,
                        link.confidence, link.decision, link.source_review_id,
                        link.carried_review_id, link.carried_class_id,
                        link.decided_by, link.decided_at,
                        link.evidence,
                        source.start_date AS source_start_date,
                        source.end_date AS source_end_date,
                        source.direction AS source_direction,
                        source.support AS source_support,
                        source_review.title AS source_title,
                        source_review.note AS source_note,
                        source_review.state AS source_review_state,
                        source_category.name AS source_category_name,
                        source_family.name AS source_family_name,
                        COALESCE(source_tags.tags, ARRAY[]::TEXT[]) AS source_tags,
                        target.start_date AS target_start_date,
                        target.end_date AS target_end_date,
                        target.direction AS target_direction,
                        target.support AS target_support,
                        target_review.title AS target_title,
                        target_review.state AS target_review_state,
                        dispatch.target_run_id, dispatch.recovery_id,
                        dispatch.completed_at
                    FROM event_reconciliation_link link
                    JOIN event_reconciliation_dispatch dispatch
                      ON dispatch.id = link.dispatch_id
                    JOIN current_run ON current_run.run_id = dispatch.target_run_id
                    LEFT JOIN event source ON source.id = link.source_event_id
                    LEFT JOIN event target ON target.id = link.target_event_id
                    LEFT JOIN event_review source_review
                      ON source_review.id = link.source_review_id
                    LEFT JOIN event_class source_class
                      ON source_class.event_id = source.id AND source_class.end_at IS NULL
                    LEFT JOIN category source_category
                      ON source_category.id = source_class.category_id
                    LEFT JOIN family source_family
                      ON source_family.id = source_class.family_id
                    LEFT JOIN LATERAL (
                        SELECT array_agg(tag.name ORDER BY tag.name) AS tags
                        FROM event_tag event_tag
                        JOIN tag ON tag.id = event_tag.tag_id
                        WHERE event_tag.review_id = source_review.id
                    ) source_tags ON true
                    LEFT JOIN LATERAL (
                        SELECT review.*
                        FROM event_review review
                        WHERE review.event_id = target.id
                          AND NOT EXISTS (
                              SELECT 1 FROM event_reconciliation_link inactive
                              WHERE inactive.carried_review_id = review.id
                                AND inactive.decision IN ('pending', 'rejected')
                          )
                        ORDER BY review.created_at DESC, review.id DESC
                        LIMIT 1
                    ) target_review ON true
                    WHERE dispatch.status = 'published'
                      AND COALESCE(source.site_id, target.site_id) = :site_id
                      AND link.source_review_id IS NOT NULL
                    ORDER BY
                        CASE link.decision
                            WHEN 'pending' THEN 0
                            WHEN 'auto_accepted' THEN 1
                            WHEN 'accepted' THEN 2
                            ELSE 3
                        END,
                        COALESCE(target.start_date, source.start_date) DESC,
                        link.id DESC
                    """
                ),
                {"site_id": site_id},
            ).mappings()
            return [dict(row) for row in rows]
    finally:
        engine.dispose()


def _deactivate_carried_annotation(connection, link: dict[str, Any]) -> set[int]:
    """Hide a system-carried review without disturbing later manager edits."""

    carried_review_id = link.get("carried_review_id")
    target_event_id = link.get("target_event_id")
    if carried_review_id is None or target_event_id is None:
        return set()
    latest_review_id = connection.execute(
        text(
            """
            SELECT id FROM event_review
            WHERE event_id = :event_id
            ORDER BY created_at DESC, id DESC LIMIT 1
            """
        ),
        {"event_id": target_event_id},
    ).scalar_one_or_none()
    if latest_review_id != carried_review_id:
        return set()
    carried_class_id = link.get("carried_class_id")
    family_id = connection.execute(
        text(
            """
            SELECT family_id FROM event_class
            WHERE id = :class_id AND event_id = :event_id AND end_at IS NULL
            FOR UPDATE
            """
        ),
        {"class_id": carried_class_id, "event_id": target_event_id},
    ).scalar_one_or_none()
    if carried_class_id is not None:
        connection.execute(
            text(
                """
                UPDATE event_class SET end_at = now()
                WHERE id = :class_id AND event_id = :event_id AND end_at IS NULL
                """
            ),
            {"class_id": carried_class_id, "event_id": target_event_id},
        )
    connection.execute(
        text("UPDATE event SET status = 'detected' WHERE id = :event_id"),
        {"event_id": target_event_id},
    )
    return {int(family_id)} if family_id is not None else set()


def _carry_annotation(
    connection, *, link_id: int, actor_id: int
) -> tuple[int | None, set[int]]:
    """Carry the source interpretation to the chosen current event once."""

    link = connection.execute(
        text(
            """
            SELECT source_review_id, target_event_id, carried_review_id,
                carried_class_id
            FROM event_reconciliation_link
            WHERE id = :link_id
            FOR UPDATE
            """
        ),
        {"link_id": link_id},
    ).mappings().one()
    if link["source_review_id"] is None or link["target_event_id"] is None:
        raise ValueError("This reconciliation does not have both an annotation and a current event.")
    if link["carried_review_id"] is not None:
        carried_review_id = int(link["carried_review_id"])
        latest_review_id = connection.execute(
            text(
                """
                SELECT id FROM event_review
                WHERE event_id = :event_id
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ),
            {"event_id": int(link["target_event_id"])},
        ).scalar_one_or_none()
        if latest_review_id != carried_review_id:
            # Confirm the provenance relationship without replacing a newer
            # interpretation the manager wrote on the recovered event.
            return carried_review_id, set()
    else:
        existing_target_review = connection.execute(
            text(
                """
                SELECT id FROM event_review
                WHERE event_id = :event_id
                ORDER BY created_at DESC, id DESC LIMIT 1
                """
            ),
            {"event_id": int(link["target_event_id"])},
        ).scalar_one_or_none()
        if existing_target_review is not None:
            # The recovered event has since been interpreted directly.  The
            # manager is confirming lineage here, not asking us to overwrite
            # that newer work with the old annotation.
            return None, set()
        carried_review_id = int(
            connection.execute(
                text(
                    """
                    INSERT INTO event_review (
                        event_id, parent_id, created_by, title, note, state
                    )
                    SELECT :target_event_id, source.id, :actor_id,
                        source.title, source.note, source.state
                    FROM event_review source
                    WHERE source.id = :source_review_id
                    RETURNING id
                    """
                ),
                {
                    "target_event_id": int(link["target_event_id"]),
                    "source_review_id": int(link["source_review_id"]),
                    "actor_id": actor_id,
                },
            ).scalar_one()
        )
        connection.execute(
            text(
                """
                INSERT INTO event_tag (review_id, tag_id)
                SELECT :carried_review_id, tag_id
                FROM event_tag WHERE review_id = :source_review_id
                ON CONFLICT (review_id, tag_id) DO NOTHING
                """
            ),
            {
                "carried_review_id": carried_review_id,
                "source_review_id": int(link["source_review_id"]),
            },
        )
    source_class = connection.execute(
        text(
            """
            SELECT category_id, family_id
            FROM event_class class
            JOIN event_review review ON review.event_id = class.event_id
            WHERE review.id = :source_review_id AND class.end_at IS NULL
            """
        ),
        {"source_review_id": int(link["source_review_id"])},
    ).mappings().one_or_none()
    affected_families: set[int] = set()
    if source_class is not None:
        previous_family = connection.execute(
            text(
                """
                SELECT family_id FROM event_class
                WHERE event_id = :event_id AND end_at IS NULL
                FOR UPDATE
                """
            ),
            {"event_id": int(link["target_event_id"])},
        ).scalar_one_or_none()
        if previous_family is not None:
            affected_families.add(int(previous_family))
        if source_class["family_id"] is not None:
            affected_families.add(int(source_class["family_id"]))
        connection.execute(
            text(
                """
                UPDATE event_class SET end_at = now()
                WHERE event_id = :event_id AND end_at IS NULL
                """
            ),
            {"event_id": int(link["target_event_id"])},
        )
        carried_class_id = int(connection.execute(
            text(
                """
                INSERT INTO event_class (
                    event_id, category_id, family_id, assigned_by,
                    assignment_source
                ) VALUES (
                    :event_id, :category_id, :family_id, :actor_id, 'user'
                )
                RETURNING id
                """
            ),
            {
                "event_id": int(link["target_event_id"]),
                "category_id": int(source_class["category_id"]),
                "family_id": source_class["family_id"],
                "actor_id": actor_id,
            },
        ).scalar_one())
    else:
        carried_class_id = None
    connection.execute(
        text(
            """
            UPDATE event_reconciliation_link
            SET carried_review_id = :carried_review_id,
                carried_class_id = :carried_class_id
            WHERE id = :link_id
            """
        ),
        {
            "link_id": link_id,
            "carried_review_id": carried_review_id,
            "carried_class_id": carried_class_id,
        },
    )
    connection.execute(
        text("UPDATE event SET status = 'reviewed' WHERE id = :event_id"),
        {"event_id": int(link["target_event_id"])},
    )
    return carried_review_id, affected_families


def save_reconciliation_decision(
    *,
    org_id: int,
    site_id: int,
    link_id: int,
    decision: str,
    actor_id: int,
    target_event_id: int | None = None,
) -> dict[str, Any]:
    """Atomically save one manager reconciliation decision.

    ``decision`` is accepted, rejected, or pending.  Passing a different
    ``target_event_id`` preserves the original proposal as rejected and creates
    a manager-directed accepted link to the selected current occurrence.
    """

    if active_gateway_scope() is not None:
        return application_call(
            "workspace",
            "events.save_reconciliation",
            org_id=org_id,
            site_id=site_id,
            link_id=link_id,
            decision=decision,
            actor_id=actor_id,
            target_event_id=target_event_id,
        )
    if decision not in {"accepted", "rejected", "pending"}:
        raise ValueError("Choose accept, reject, or require confirmation.")
    engine = _engine()
    if engine is None:
        raise RuntimeError("The local database service is unavailable.")
    affected_families: set[int] = set()
    active_link_id = link_id
    try:
        with engine.begin() as connection:
            link = connection.execute(
                text(
                    """
                    SELECT link.*, dispatch.target_run_id,
                        COALESCE(source.site_id, target.site_id) AS site_id,
                        site.org_id
                    FROM event_reconciliation_link link
                    JOIN event_reconciliation_dispatch dispatch
                      ON dispatch.id = link.dispatch_id
                    LEFT JOIN event source ON source.id = link.source_event_id
                    LEFT JOIN event target ON target.id = link.target_event_id
                    JOIN site ON site.id = COALESCE(source.site_id, target.site_id)
                    WHERE link.id = :link_id AND dispatch.status = 'published'
                    FOR UPDATE OF link
                    """
                ),
                {"link_id": link_id},
            ).mappings().one_or_none()
            if (
                link is None
                or int(link["site_id"]) != site_id
                or int(link["org_id"]) != org_id
            ):
                raise ValueError("That reconciliation is no longer available for this site.")

            old_decision = str(link["decision"])
            old_target_id = link["target_event_id"]
            if target_event_id is not None and target_event_id != old_target_id:
                valid_target = connection.execute(
                    text(
                        """
                        SELECT id FROM current_event
                        WHERE id = :target_event_id AND site_id = :site_id
                          AND run_id = :target_run_id
                        """
                    ),
                    {
                        "target_event_id": target_event_id,
                        "site_id": site_id,
                        "target_run_id": int(link["target_run_id"]),
                    },
                ).scalar_one_or_none()
                if valid_target is None:
                    raise ValueError("Choose an occurrence from the current recovered forecast.")
                affected_families |= _deactivate_carried_annotation(connection, dict(link))
                connection.execute(
                    text(
                        """
                        UPDATE event_reconciliation_link
                        SET decision = 'rejected', decided_by = :actor_id,
                            decided_at = now()
                        WHERE id = :link_id
                        """
                    ),
                    {"link_id": link_id, "actor_id": actor_id},
                )
                active_link_id = int(
                    connection.execute(
                        text(
                            """
                            INSERT INTO event_reconciliation_link (
                                dispatch_id, source_event_id, target_event_id,
                                relationship, confidence, decision,
                                source_review_id, decided_by, decided_at, evidence
                            ) VALUES (
                                :dispatch_id, :source_event_id, :target_event_id,
                                'same', 1, 'accepted', :source_review_id,
                                :actor_id, now(), CAST(:evidence AS JSONB)
                            )
                            ON CONFLICT (
                                dispatch_id, COALESCE(source_event_id, 0),
                                COALESCE(target_event_id, 0), relationship
                            ) DO UPDATE SET
                                decision = 'accepted', decided_by = EXCLUDED.decided_by,
                                decided_at = EXCLUDED.decided_at,
                                source_review_id = EXCLUDED.source_review_id,
                                evidence = EXCLUDED.evidence
                            RETURNING id
                            """
                        ),
                        {
                            "dispatch_id": int(link["dispatch_id"]),
                            "source_event_id": link["source_event_id"],
                            "target_event_id": target_event_id,
                            "source_review_id": link["source_review_id"],
                            "actor_id": actor_id,
                            "evidence": json.dumps(
                                {
                                    "manager_redirected_from_link_id": link_id,
                                    "manager_redirected_from_target_event_id": old_target_id,
                                },
                                sort_keys=True,
                            ),
                        },
                    ).scalar_one()
                )
                decision = "accepted"
            else:
                if decision in {"pending", "rejected"}:
                    affected_families |= _deactivate_carried_annotation(connection, dict(link))
                connection.execute(
                    text(
                        """
                        UPDATE event_reconciliation_link
                        SET decision = :decision, decided_by = :actor_id,
                            decided_at = now()
                        WHERE id = :link_id
                        """
                    ),
                    {"link_id": link_id, "decision": decision, "actor_id": actor_id},
                )

            carried_review_id = None
            if decision == "accepted":
                carried_review_id, new_families = _carry_annotation(
                    connection, link_id=active_link_id, actor_id=actor_id
                )
                affected_families |= new_families
            connection.execute(
                text(
                    """
                    INSERT INTO audit (
                        org_id, site_id, actor_id, actor_name, actor_role,
                        action, object_type, object_id, change_set, source
                    ) VALUES (
                        :org_id, :site_id, :actor_id,
                        'Credent manager', 'manager',
                        'decide_event_reconciliation', 'event_reconciliation_link',
                        :object_id, CAST(:change_set AS JSONB), 'streamlit'
                    )
                    """
                ),
                {
                    "org_id": org_id,
                    "site_id": site_id,
                    "actor_id": actor_id,
                    "object_id": str(active_link_id),
                    "change_set": json.dumps(
                        {
                            "source_link_id": link_id,
                            "old_decision": old_decision,
                            "decision": decision,
                            "old_target_event_id": old_target_id,
                            "target_event_id": target_event_id or old_target_id,
                            "carried_review_id": carried_review_id,
                        },
                        sort_keys=True,
                    ),
                },
            )
    finally:
        engine.dispose()

    queued_runs: list[int] = []
    queue_errors: list[str] = []
    for family_id in sorted(affected_families):
        try:
            run_id, _ = refresh_family(
                org_id=org_id,
                family_id=family_id,
                actor_id=actor_id,
            )
        except Exception as error:
            queue_errors.append(str(error))
        else:
            queued_runs.append(run_id)
    return {
        "link_id": active_link_id,
        "decision": decision,
        "family_run_ids": queued_runs,
        "family_queue_errors": queue_errors,
    }


__all__ = [
    "DECISION_LABELS",
    "load_event_reconciliations",
    "save_reconciliation_decision",
]
