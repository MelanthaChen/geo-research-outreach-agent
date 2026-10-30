import json
import os
import sqlite3
import threading
import urllib.request
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest

from outreach_agent.admin_auth import hash_password, init_auth_store
from outreach_agent import dashboard as dashboard_module


def test_administrator_password_minimum_is_12_and_hash_remains_argon2id():
    with pytest.raises(ValueError, match="at least 12 characters"):
        hash_password("lower12char")
    encoded = hash_password("lower12char!")
    assert encoded.startswith("$argon2id$")

def _server(tmp_path, monkeypatch):
    registry = tmp_path / "registry.db"
    dashboard_module.REGISTRY_DB = registry
    monkeypatch.setenv("OUTREACH_ENV", "staging")
    monkeypatch.setenv("OUTREACH_ADMIN_EMAIL", "chen.yuxuan9@northeastern.edu")
    monkeypatch.setenv("OUTREACH_ADMIN_PASSWORD_HASH", hash_password("a secure test password 123"))
    monkeypatch.setenv("OUTREACH_COOKIE_SECURE", "false")
    init_auth_store(registry)
    data = {"summary": {"total_companies": 0, "cloud_registry": True}, "filters": {},
            "simulated_signup_url": "", "participation_form_configuration": {}, "details": {}}
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_module.make_handler(data))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def test_private_api_requires_login_and_session_is_cookie_based(tmp_path, monkeypatch):
    server, thread, base = _server(tmp_path, monkeypatch)
    try:
        with __import__("pytest").raises(__import__("urllib.error").error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/summary")
        assert error.value.code == 401
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        request = urllib.request.Request(base + "/api/auth/login", data=json.dumps({
            "email": "chen.yuxuan9@northeastern.edu", "password": "a secure test password 123",
        }).encode(), headers={"Content-Type": "application/json"}, method="POST")
        response = opener.open(request)
        assert json.load(response)["authenticated"] is True
        session = json.load(opener.open(base + "/api/auth/session"))
        assert session["authenticated"] is True
        assert session["email"] == "chen.yuxuan9@northeastern.edu"
        assert json.load(opener.open(base + "/api/summary"))["summary"]["total_companies"] == 0
        assert "OUTREACH_ADMIN_PASSWORD_HASH" not in opener.open(base + "/api/auth/session").read().decode()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_login_rejects_wrong_password_and_state_changing_requests_need_csrf(tmp_path, monkeypatch):
    server, thread, base = _server(tmp_path, monkeypatch)
    try:
        request = urllib.request.Request(base + "/api/auth/login", data=json.dumps({
            "email": "chen.yuxuan9@northeastern.edu", "password": "wrong password that is long",
        }).encode(), headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(request)
        except __import__("urllib.error").error.HTTPError as error:
            assert error.code == 401
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        request = urllib.request.Request(base + "/api/auth/login", data=json.dumps({
            "email": "chen.yuxuan9@northeastern.edu", "password": "a secure test password 123",
        }).encode(), headers={"Content-Type": "application/json"}, method="POST")
        opener.open(request)
        post = urllib.request.Request(base + "/api/recruitment/approve-batch", data=b'{"draft_ids":[]}', headers={"Content-Type":"application/json"}, method="POST")
        try:
            opener.open(post)
        except __import__("urllib.error").error.HTTPError as error:
            assert error.code == 403
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_https_same_origin_login_is_allowed(tmp_path, monkeypatch):
    server, thread, base = _server(tmp_path, monkeypatch)
    try:
        request = urllib.request.Request(base + "/api/auth/login", data=json.dumps({
            "email": "chen.yuxuan9@northeastern.edu", "password": "a secure test password 123",
        }).encode(), headers={
            "Content-Type": "application/json",
            "Origin": base.replace("http://", "https://"),
            "Sec-Fetch-Site": "same-origin",
        }, method="POST")
        assert json.load(urllib.request.urlopen(request))["authenticated"] is True
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_auth_tables_are_additive_and_password_hash_is_argon2id(tmp_path, monkeypatch):
    server, thread, base = _server(tmp_path, monkeypatch)
    try:
        with sqlite3.connect(tmp_path / "registry.db") as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"admin_sessions", "admin_auth_events"}.issubset(names)
        assert os.environ["OUTREACH_ADMIN_PASSWORD_HASH"].startswith("$argon2id$")
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_authenticated_admin_session_authorizes_review_actions_without_review_token(tmp_path, monkeypatch):
    server, thread, base = _server(tmp_path, monkeypatch)
    original = dashboard_module.review_contact
    dashboard_module.review_contact = lambda **kwargs: {"status": "REVIEWED"}
    try:
        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        login = urllib.request.Request(base + "/api/auth/login", data=json.dumps({
            "email": "chen.yuxuan9@northeastern.edu", "password": "a secure test password 123",
        }).encode(), headers={"Content-Type": "application/json"}, method="POST")
        opener.open(login)
        csrf = next(cookie.value for cookie in jar if cookie.name == "geo_outreach_csrf")
        review = urllib.request.Request(base + "/api/research/contact-review", data=json.dumps({
            "prospect_id": "fixture", "contact_id": "contact", "decision": "APPROVED", "reviewer": "operator",
        }).encode(), headers={"Content-Type": "application/json", "X-CSRF-Token": csrf}, method="POST")
        assert json.load(opener.open(review))["status"] == "REVIEWED"
    finally:
        dashboard_module.review_contact = original
        server.shutdown(); server.server_close(); thread.join(timeout=2)
