"""Regression checks for the one-round-trip appointment booking path."""

import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db


class _Result:
    def __init__(self, data):
        self.data = data


class _RpcCall:
    def __init__(self, data):
        self._data = data

    async def execute(self):
        return _Result(self._data)


class _FakeDb:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _RpcCall(self.data)


def test_available_slot_uses_one_rpc() -> None:
    fake = _FakeDb([{"result_status": "booked"}])
    original_adb = db._adb

    async def fake_adb():
        return fake

    db._adb = fake_adb
    try:
        booking_id = asyncio.run(
            db.insert_appointment("Lead", "+919999999999", "2099-01-02", "16:00", "Site visit")
        )
    finally:
        db._adb = original_adb

    assert booking_id
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == "book_appointment_atomic"


def test_unavailable_slot_returns_rpc_alternative() -> None:
    fake = _FakeDb([{
        "result_status": "unavailable",
        "next_date": "2099-01-02",
        "next_time": "17:00",
    }])
    original_adb = db._adb

    async def fake_adb():
        return fake

    db._adb = fake_adb
    try:
        try:
            asyncio.run(
                db.insert_appointment("Lead", "+919999999999", "2099-01-02", "16:00", "Site visit")
            )
        except db.SlotUnavailableError as exc:
            assert exc.next_slot == "2099-01-02 at 17:00"
        else:
            raise AssertionError("unavailable slot was incorrectly booked")
    finally:
        db._adb = original_adb

    assert len(fake.calls) == 1


if __name__ == "__main__":
    test_available_slot_uses_one_rpc()
    test_unavailable_slot_returns_rpc_alternative()
    print("Atomic booking checks passed.")
