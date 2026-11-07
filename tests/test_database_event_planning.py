from __future__ import annotations

from datetime import date

from database_event_planning import _initial_working


def test_initial_working_prefers_saved_draft_and_preserves_fit_lineage() -> None:
    workspace = {
        "plans": [
            {
                "id": 2,
                "status": "draft",
                "placements": [
                    {
                        "id": 11,
                        "title": "Payday Friday",
                        "family_name": "Payday",
                        "family_id": 7,
                        "family_estimate_id": 23,
                        "start_date": date(2026, 10, 20),
                        "scale": 1.1,
                        "repeat": {"occurrence": "One time"},
                        "effects": [
                            {"day": 0, "low": 80, "median": 100, "high": 130}
                        ],
                    }
                ],
            },
            {"id": 1, "status": "implemented", "placements": []},
        ]
    }

    working = _initial_working(workspace)

    assert working == [
        {
            "title": "Payday Friday",
            "family_id": 7,
            "family_estimate_id": 23,
            "start_date": date(2026, 10, 20),
            "scale": 1.1,
            "repeat": {"occurrence": "One time"},
            "effects": [
                {"day": 0, "low": 80.0, "median": 100.0, "high": 130.0}
            ],
        }
    ]
