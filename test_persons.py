"""Person provenance and transparent timeline behavior."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_person_requires_traceable_source(client: TestClient, diary_factory) -> None:
    diary = diary_factory("今天和小林一起吃午饭。")
    response = client.post(
        "/api/v1/persons",
        json={
            "display_name": "小林",
            "source_entry_id": diary["id"],
            "source_revision_id": diary["current_revision_id"],
            "source_evidence": "小林",
        },
    )
    assert response.status_code == 201
    person = response.json()
    assert person["display_name"] == "小林"
    assert person["source_entry_id"] == diary["id"]
    assert person["source_revision_id"] == diary["current_revision_id"]


def test_person_with_fabricated_source_evidence_is_rejected(client: TestClient, diary_factory) -> None:
    diary = diary_factory("今天下雨了。")
    response = client.post(
        "/api/v1/persons",
        json={
            "display_name": "小王",
            "source_entry_id": diary["id"],
            "source_revision_id": diary["current_revision_id"],
            "source_evidence": "小王说他要来",
        },
    )
    assert response.status_code == 422


def test_person_with_wrong_revision_is_rejected(client: TestClient, diary_factory) -> None:
    first = diary_factory("第一条日记。")
    second = diary_factory("第二条日记。")
    response = client.post(
        "/api/v1/persons",
        json={
            "display_name": "小李",
            "source_entry_id": first["id"],
            "source_revision_id": second["current_revision_id"],
        },
    )
    assert response.status_code == 422


def test_person_timeline_is_literal_and_does_not_merge_identities(client: TestClient, diary_factory) -> None:
    one = diary_factory("今天小林说她升职了。")
    two = diary_factory("另一个故事里的小林喜欢旅行。")
    created = client.post(
        "/api/v1/persons",
        json={
            "display_name": "小林",
            "aliases": ["林林"],
            "source_entry_id": one["id"],
            "source_revision_id": one["current_revision_id"],
            "source_evidence": "小林",
        },
    )
    assert created.status_code == 201
    timeline = client.get(f"/api/v1/persons/{created.json()['id']}/timeline").json()
    assert timeline["method"] == "literal_text_match"
    assert "does not prove identity" in timeline["note"]
    assert len(timeline["items"]) == 2
    assert {item["entry_id"] for item in timeline["items"]} == {one["id"], two["id"]}


def test_duplicate_person_from_same_revision_is_conflict(client: TestClient, diary_factory) -> None:
    diary = diary_factory("小王今天来了。")
    payload = {
        "display_name": "小王",
        "source_entry_id": diary["id"],
        "source_revision_id": diary["current_revision_id"],
    }
    assert client.post("/api/v1/persons", json=payload).status_code == 201
    duplicate = client.post("/api/v1/persons", json=payload)
    assert duplicate.status_code == 409
