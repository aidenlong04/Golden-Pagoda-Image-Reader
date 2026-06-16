from __future__ import annotations

import json

import records_index as ri


def test_empty_load_missing_file(tmp_path):
    p = tmp_path / "records_index.json"
    index = ri.load_index(p)
    assert index["version"] == 1
    assert index["channel_id"] is None
    assert index["users"] == {}


def test_add_record_roundtrip(tmp_path):
    p = tmp_path / "records_index.json"
    assert ri.add_record(10, 1000, channel_id=99, path=p) is True
    assert ri.add_record(10, 1001, channel_id=99, path=p) is True
    assert ri.add_record(20, 2000, channel_id=99, path=p) is True

    assert ri.get_record_message_ids(10, path=p) == [1000, 1001]
    assert ri.get_record_message_ids(20, path=p) == [2000]
    assert ri.get_record_message_ids(30, path=p) == []
    assert ri.get_channel_id(path=p) == 99


def test_add_record_dedups(tmp_path):
    p = tmp_path / "records_index.json"
    ri.add_record(10, 1000, path=p)
    ri.add_record(10, 1000, path=p)
    assert ri.get_record_message_ids(10, path=p) == [1000]


def test_index_add_in_memory_then_save(tmp_path):
    p = tmp_path / "records_index.json"
    index = ri.load_index(p)
    for mid in (5, 6, 7, 6):  # 6 repeated -> deduped
        ri.index_add(index, 42, mid, channel_id=77)
    assert ri.save_index(index, p) is True

    reread = ri.load_index(p)
    assert reread["users"]["42"] == [5, 6, 7]
    assert reread["channel_id"] == 77


def test_load_corrupt_file_is_failsoft(tmp_path):
    p = tmp_path / "records_index.json"
    p.write_text("not json {", encoding="utf-8")
    index = ri.load_index(p)
    assert index["users"] == {}
    assert index["version"] == 1


def test_load_non_dict_users_resets(tmp_path):
    p = tmp_path / "records_index.json"
    p.write_text(json.dumps({"users": ["bad"]}), encoding="utf-8")
    index = ri.load_index(p)
    assert index["users"] == {}


def test_get_message_ids_coerces_str_ids(tmp_path):
    p = tmp_path / "records_index.json"
    p.write_text(
        json.dumps({"version": 1, "channel_id": 1, "users": {"5": ["10", 11]}}),
        encoding="utf-8",
    )
    assert ri.get_record_message_ids(5, path=p) == [10, 11]
