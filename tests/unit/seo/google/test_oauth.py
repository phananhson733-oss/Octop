"""Google consent URL, ID token checks, return paths and stored credentials."""

from urllib.parse import parse_qs, urlsplit

import jwt
import pytest

from octop.infra.seo.google import oauth

NOW = 1_800_000_000


def _id_token(**claims):
    base = {
        "iss": "https://accounts.google.com",
        "aud": "client-1",
        "sub": "g-1",
        "email": "owner@example.com",
        "exp": NOW + 60,
    }
    return jwt.encode(base | claims, "unused-signing-key-of-32-bytes!!", algorithm="HS256")


def test_consent_url_asks_offline_for_both_read_only_scopes():
    url = oauth.authorize_url(
        client_id="client-1", redirect_uri="https://o.test/cb", state="s", code_challenge="c"
    )
    parts = urlsplit(url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == oauth.AUTH_URL
    assert query["scope"] == (
        "openid email https://www.googleapis.com/auth/webmasters.readonly "
        "https://www.googleapis.com/auth/analytics.readonly"
    )
    assert query["access_type"] == "offline" and "consent" in query["prompt"].split()
    assert (query["state"], query["code_challenge"], query["code_challenge_method"]) == (
        "s",
        "c",
        "S256",
    )


def test_id_token_must_be_ours_from_google_and_fresh():
    assert oauth.google_account(_id_token(), "client-1", NOW) == ("g-1", "owner@example.com")
    assert oauth.google_account(_id_token(aud=["x", "client-1"], email=""), "client-1", NOW) == (
        "g-1",
        None,
    )
    for bad in (
        _id_token(aud="other"),
        _id_token(iss="https://evil.example"),
        _id_token(exp=NOW - 1),
        _id_token(sub=""),
        "not-a-jwt",
    ):
        with pytest.raises(oauth.GoogleRejected):
            oauth.google_account(bad, "client-1", NOW)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/workbench/seo-workbench.main?project=p", "/workbench/seo-workbench.main?project=p"),
        (None, "/workbench"),
        ("", "/workbench"),
        ("workbench", "/workbench"),
        ("//evil.example/x", "/workbench"),
        ("/\\evil.example", "/workbench"),
        ("https://evil.example/x", "/workbench"),
    ],
)
def test_only_same_origin_paths_are_returned_to(raw, expected):
    assert oauth.safe_return_path(raw, "/workbench") == expected


def test_credentials_keep_the_old_refresh_token_when_google_omits_it():
    fresh = oauth.credentials({"access_token": "a", "expires_in": 100}, NOW, "old")
    assert fresh == {"access_token": "a", "refresh_token": "old", "expires_at": NOW + 100}
    replaced = oauth.credentials({"access_token": "a", "refresh_token": "new"}, NOW, "old")
    assert (replaced["refresh_token"], replaced["expires_at"]) == ("new", NOW + 3600)
    assert oauth.needs_refresh(fresh, NOW + 100 - oauth.REFRESH_SKEW_S)
    assert not oauth.needs_refresh(fresh, NOW)
    assert oauth.needs_refresh({}, NOW)


def test_an_omitted_scope_means_everything_asked_for():
    assert oauth.granted_scopes({}) == list(oauth.SCOPES)
    assert oauth.granted_scopes({"scope": "openid email"}) == ["openid", "email"]
