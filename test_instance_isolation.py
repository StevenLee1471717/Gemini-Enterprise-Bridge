"""Separate Chrome accounts must keep their service state independent."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from config import BridgeSettings
from test_responses import browser


def test_account_instances_isolate_identical_browser_and_session_ids(tmp_path, monkeypatch):
    settings = []
    for name, port in (("primary", 8000), ("secondary", 8001)):
        root = tmp_path / name
        for key, value in {
            "BRIDGE_PORT": str(port),
            "BRIDGE_DEFAULT_MODEL": "gemini-4-carbon",
            "BRIDGE_API_KEY": "",
            "BRIDGE_AUTO_EXPORT_EXTENSION": "false",
            "BRIDGE_AUTO_SPAWN_TABS": "false",
            "BRIDGE_AUTO_RESET_NEW_CHAT": "false",
            "BRIDGE_GENERATED_DIR": str(root / "generated"),
            "BRIDGE_RESPONSE_STORE_DIR": str(root / "responses"),
            "BRIDGE_SESSION_ARCHIVE_PATH": str(root / "sessions.sqlite3"),
            "BRIDGE_USAGE_LEDGER_PATH": str(root / "usage.json"),
            "BRIDGE_DISTRIBUTION_PATH": str(root / "distribution.json"),
        }.items():
            monkeypatch.setenv(key, value)
        settings.append(asdict(BridgeSettings.from_env(base_dir=root)))

    def converse(client, marker):
        first = client.post("/v1/responses", json={
            "input": marker, "prompt_cache_key": "same-session-id",
        })
        assert first.status_code == 200, first.text
        second = client.post("/v1/responses", json={
            "input": "repeat my code", "previous_response_id": first.json()["id"],
        })
        assert second.status_code == 200, second.text
        assert second.json()["output"][0]["content"][0]["text"] == marker
        return first.json()["id"]

    # Both simulated extensions use client_id=test and tab_id=7.
    with browser(["ORIGINAL-ONLY"] * 2, **settings[0]) as (left, left_commands), \
         browser(["SECONDARY-ONLY"] * 2, **settings[1]) as (right, right_commands):
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(converse, left, "ORIGINAL-ONLY")
            b = pool.submit(converse, right, "SECONDARY-ONLY")
            a_id, b_id = a.result(), b.result()
        assert left.get(f"/v1/responses/{b_id}").status_code == 404
        assert right.get(f"/v1/responses/{a_id}").status_code == 404
        assert all("SECONDARY-ONLY" not in cmd["prompt"] for cmd in left_commands)
        assert all("ORIGINAL-ONLY" not in cmd["prompt"] for cmd in right_commands)
        for own, other, content in ((left, right, b"original file"), (right, left, b"secondary file")):
            uploaded = own.post("/v1/files", files={"file": ("probe.txt", content, "text/plain")})
            assert uploaded.status_code == 200
            file_id = uploaded.json()["id"]
            assert own.get(f"/v1/files/{file_id}/content").content == content
            assert other.get(f"/v1/files/{file_id}/content").status_code == 404
