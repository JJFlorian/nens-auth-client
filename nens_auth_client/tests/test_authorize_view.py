from authlib.integrations.base_client.errors import OAuthError
from authlib.jose.errors import JoseError
from django.conf import settings
from django.contrib.auth.models import User
from nens_auth_client import models
from nens_auth_client import views
from urllib.parse import parse_qs

import pytest
import time


@pytest.fixture
def users_m(mocker):
    return mocker.patch("nens_auth_client.views.users")


@pytest.fixture
def login_m(mocker):
    return mocker.patch("nens_auth_client.views.django_auth.login")


@pytest.fixture
def invite_getter(mocker):
    manager = mocker.patch("nens_auth_client.views.Invite.objects")
    return manager.select_related.return_value.get


def test_authorize(
    id_token_generator,
    auth_req_generator,
    rq_mocker,
    openid_configuration,
    users_m,
    login_m,
):
    id_token, claims = id_token_generator(testclaim="bar")
    user = User(username="testuser")
    request = auth_req_generator(id_token, user=user)
    response = views.authorize(request)
    assert response.status_code == 302  # 302 redirect to success url: all checks passed
    assert response.url == "http://testserver/success"

    # pick the token request (from the JWKS and OpenID Discovery requests)
    token_request = next(
        request
        for request in rq_mocker.request_history
        if request.url == openid_configuration["token_endpoint"]
    )
    assert token_request.timeout == settings.NENS_AUTH_TIMEOUT
    qs = parse_qs(token_request.text)
    assert qs["grant_type"] == ["authorization_code"]
    assert qs["code"] == ["code"]
    assert qs["state"] == ["state"]

    # check if Cache-Control header is set to "no-store"
    assert response._headers["cache-control"] == ("Cache-Control", "no-store")

    # check if login was called
    login_m.assert_called_with(request, user)

    # check if update_user was called
    users_m.update_user.assert_called_with(user, claims)


def test_authorize_with_invite_existing_user(
    id_token_generator,
    auth_req_generator,
    rq_mocker,
    openid_configuration,
    users_m,
    login_m,
    invite_getter,
):
    # Attach an invite that is associated to a user. That user should be
    # logged in and associated to the remote.
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, user=None)

    user = User(username="testuser")
    invite_getter.return_value = models.Invite(id="foo", user=user)
    request.session[views.INVITE_ID_KEY] = "foo"

    response = views.authorize(request)
    assert response.status_code == 302  # 302 redirect to success url: all checks passed
    assert response.url == "http://testserver/success"

    # check if the invite was looked up
    invite_getter.assert_called_with(id="foo", status=models.Invite.PENDING)

    # check if create_remote_user was called
    users_m.create_remote_user.assert_called_with(user, claims)

    # check if login was called
    login_m.assert_called_with(request, user)

    # check if update_user was called
    users_m.update_user.assert_called_with(user, claims)


def test_authorize_wrong_nonce(id_token_generator, auth_req_generator):
    # The id token has a different nonce than the session
    id_token, claims = id_token_generator(nonce="a")
    request = auth_req_generator(id_token, nonce="b")
    with pytest.raises(JoseError):
        views.authorize(request)


def test_authorize_wrong_state(id_token_generator, auth_req_generator):
    # The incoming state query param is different from the session
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, state="a")
    request.session["_cognito_authlib_state_"] = "b"
    with pytest.raises(OAuthError):
        views.authorize(request)


def test_authorize_wrong_issuer(id_token_generator, auth_req_generator):
    # The issuer in the id token is unknown
    id_token, claims = id_token_generator(iss="https://google.com")
    request = auth_req_generator(id_token)
    with pytest.raises(JoseError):
        views.authorize(request)


def test_authorize_wrong_audience(id_token_generator, auth_req_generator):
    # The audience in the id token is not equal to client_id
    id_token, claims = id_token_generator(aud="abcd")
    request = auth_req_generator(id_token)
    with pytest.raises(JoseError):
        views.authorize(request)


def test_authorize_expired(id_token_generator, auth_req_generator):
    # The id token has expired
    # Note that authlib has a 120 seconds "leeway" (for clock skew)
    id_token, claims = id_token_generator(exp=int(time.time()) - 121)
    request = auth_req_generator(id_token)
    with pytest.raises(JoseError):
        views.authorize(request)


def test_authorize_corrupt_signature(id_token_generator, auth_req_generator):
    # The id token has invalid signature padding
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token[:-1])
    with pytest.raises(JoseError):
        views.authorize(request)


def test_authorize_bad_signature(id_token_generator, auth_req_generator):
    # The id token has invalid signature
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token[:-16])
    with pytest.raises(JoseError):
        views.authorize(request)


def test_authorize_unsigned_token(id_token_generator, auth_req_generator):
    # The id token has no signature
    id_token, claims = id_token_generator(alg="none")
    request = auth_req_generator(id_token)
    with pytest.raises(JoseError):
        views.authorize(request)


def test_authorize_invalid_key_id(id_token_generator, auth_req_generator):
    # The id token is signed with an unknown key
    id_token, claims = id_token_generator(kid="unknown_key_id")
    request = auth_req_generator(id_token)
    with pytest.raises(ValueError):
        views.authorize(request)


def test_authorize_error(rf):
    # The authorization endpoint (on the authorization server) may give a
    # redirect (302) with an error message.
    request = rf.get("http://testserver/authorize/?error=some_error")
    request.session = {}
    with pytest.raises(OAuthError, match="some_error: some_error"):
        views.authorize(request)


def test_authorize_error_with_description(rf):
    request = rf.get(
        "http://testserver/authorize/?error=some_error&error_description=bla"
    )
    request.session = {}
    with pytest.raises(OAuthError, match="some_error: bla"):
        views.authorize(request)


def test_token_error(rq_mocker, rf, openid_configuration):
    rq_mocker.post(
        openid_configuration["token_endpoint"],
        status_code=400,
        json={"error": "some_error", "error_description": "bla"},
    )
    # Create the request
    request = rf.get("http://testserver/authorize/?code=abc&state=my_state")
    request.session = {"_cognito_authlib_state_": "my_state"}
    with pytest.raises(OAuthError, match="some_error: bla"):
        views.authorize(request)
