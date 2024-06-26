from authlib.integrations.base_client.errors import OAuthError
from authlib.jose.errors import JoseError
from datetime import timedelta
from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from nens_auth_client import models
from nens_auth_client import views
from nens_auth_client.views import LOGIN_REDIRECT_SESSION_KEY
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest
import re
import time


@pytest.fixture
def users_m(mocker):
    return mocker.patch("nens_auth_client.views.users")


@pytest.fixture
def login_m(mocker):
    return mocker.patch("nens_auth_client.views.django_auth.login")


@pytest.fixture
def invitation_getter(mocker):
    manager = mocker.patch("nens_auth_client.views.Invitation.objects")
    return manager.select_related.return_value.get


@pytest.fixture
def permissions_m(mocker):
    return mocker.patch("nens_auth_client.views.permissions")


def test_authorize(
    id_token_generator,
    auth_req_generator,
    rq_mocker,
    openid_configuration,
    users_m,
    login_m,
    permissions_m,
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

    # check Cache-Control headers: page should never be cached
    pattern = "max-age=0, no-cache, no-store, must-revalidate(, private)?$"
    assert re.match(pattern, response["cache-control"]) is not None

    # check if login was called
    login_m.assert_called_with(request, user)

    # check if update_user was called
    users_m.update_user.assert_called_with(user, claims)
    args, kwargs = users_m.update_remote_user.call_args
    assert args[0] == claims
    assert args[1].keys() == {"id_token"}

    # check if auto_assign_permissions was called
    permissions_m.auto_assign_permissions.assert_called_once_with(user, claims)


def test_authorize_no_user(id_token_generator, auth_req_generator, users_m, login_m):
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, user=None)

    with pytest.raises(PermissionDenied, match="No user account available.*"):
        views.authorize(request)

    assert not login_m.called
    assert not users_m.create_user.called
    assert not users_m.create_remote_user.called
    assert not users_m.update_user.called


def test_authorize_no_redirect_in_session(
    id_token_generator, auth_req_generator, users_m, login_m, invitation_getter
):
    id_token, claims = id_token_generator(testclaim="bar")
    user = User(username="testuser")
    request = auth_req_generator(id_token, user=user)

    # delete the redirect from the session
    del request.session[views.LOGIN_REDIRECT_SESSION_KEY]

    response = views.authorize(request)

    # successful authorize should redirect to the default success url:
    assert response.status_code == 302
    assert response.url == settings.NENS_AUTH_DEFAULT_SUCCESS_URL


def test_authorize_with_invitation_existing_user(
    id_token_generator, auth_req_generator, users_m, login_m, invitation_getter
):
    # Attach an invitation that is associated to a user. That user should be
    # logged in and associated to the remote.
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, user=None)

    user = User(username="testuser")
    invitation_getter.return_value = models.Invitation(
        slug="foo", user=user, created_at=timezone.now(), email=claims["email"]
    )
    request.session[views.INVITATION_KEY] = "foo"

    response = views.authorize(request)
    assert response.status_code == 302  # 302 redirect to success url: all checks passed
    assert response.url == "http://testserver/success"

    # check if the invitation was looked up
    invitation_getter.assert_called_with(slug="foo")

    # check if create_remote_user was called
    users_m.create_remote_user.assert_called_with(user, claims)

    # check if login was called
    login_m.assert_called_with(request, user)
    assert user.backend == "nens_auth_client.backends.RemoteUserBackend"

    # check if update_user was called
    users_m.update_user.assert_called_with(user, claims)
    args, kwargs = users_m.update_remote_user.call_args
    assert args[0] == claims
    assert args[1].keys() == {"id_token"}


def test_authorize_with_invitation_new_user(
    id_token_generator,
    auth_req_generator,
    rq_mocker,
    openid_configuration,
    users_m,
    login_m,
    invitation_getter,
):
    # Attach an invitation that is associated to a user. That user should be
    # logged in and associated to the remote.
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, user=None)

    user = User(username="testuser")
    invitation_getter.return_value = models.Invitation(
        slug="foo", user=None, created_at=timezone.now(), email=claims["email"]
    )
    users_m.create_user.return_value = user
    request.session[views.INVITATION_KEY] = "foo"

    response = views.authorize(request)
    assert response.status_code == 302  # 302 redirect to success url: all checks passed
    assert response.url == "http://testserver/success"

    # check if the invitation was looked up
    invitation_getter.assert_called_with(slug="foo")

    # check if create_remote_user was called
    users_m.create_user.assert_called_with(claims)

    # check if login was called
    login_m.assert_called_with(request, user)
    assert user.backend == "nens_auth_client.backends.RemoteUserBackend"

    # check if update_user was called
    users_m.update_user.assert_called_with(user, claims)
    args, kwargs = users_m.update_remote_user.call_args
    assert args[0] == claims
    assert args[1].keys() == {"id_token"}


def test_authorize_with_invitation_email_unverified(
    id_token_generator, auth_req_generator, users_m, login_m, invitation_getter
):
    # It does not matter whether email is verified for checking invite email
    id_token, claims = id_token_generator()
    claims["email_verified"] = False
    request = auth_req_generator(id_token, user=None)

    request.session[views.INVITATION_KEY] = "foo"
    invitation_getter.return_value = models.Invitation(
        created_at=timezone.now(), email=claims["email"]
    )

    response = views.authorize(request)

    assert response.status_code == 302
    assert response.url == "http://testserver/success"


def test_authorize_with_nonexisting_invitation(
    id_token_generator, auth_req_generator, users_m, login_m, invitation_getter
):
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, user=None)

    request.session[views.INVITATION_KEY] = "foo"
    invitation_getter.side_effect = models.Invitation.DoesNotExist

    with pytest.raises(PermissionDenied, match=".*invitation does not exist.*"):
        views.authorize(request)

    invitation_getter.assert_called_with(slug="foo")
    assert not login_m.called
    assert not users_m.create_user.called
    assert not users_m.create_remote_user.called
    assert not users_m.update_user.called


def test_authorize_with_nonacceptable_invitation(
    id_token_generator, auth_req_generator, users_m, login_m, invitation_getter
):
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, user=None)

    request.session[views.INVITATION_KEY] = "foo"
    invitation_getter.return_value = models.Invitation(
        status=models.Invitation.ACCEPTED
    )

    with pytest.raises(PermissionDenied, match=".*has been used already.*"):
        views.authorize(request)

    invitation_getter.assert_called_with(slug="foo")
    assert not login_m.called
    assert not users_m.create_user.called
    assert not users_m.create_remote_user.called
    assert not users_m.update_user.called


def test_authorize_with_expired_invitation(
    id_token_generator, auth_req_generator, users_m, login_m, invitation_getter
):
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, user=None)

    request.session[views.INVITATION_KEY] = "foo"
    invitation_getter.return_value = models.Invitation(
        created_at=timezone.now() - timedelta(days=14)
    )

    with pytest.raises(PermissionDenied, match=".*has expired.*"):
        views.authorize(request)

    invitation_getter.assert_called_with(slug="foo")
    assert not login_m.called
    assert not users_m.create_user.called
    assert not users_m.create_remote_user.called
    assert not users_m.update_user.called


def test_authorize_with_mismatching_invitation(
    id_token_generator, auth_req_generator, users_m, login_m, invitation_getter
):
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, user=None)

    request.session[views.INVITATION_KEY] = "foo"
    invitation_getter.return_value = models.Invitation(
        created_at=timezone.now(), email="some@other.email"
    )

    with pytest.raises(
        PermissionDenied, match=".*intended for a user with email 'some@other.email'.*"
    ):
        views.authorize(request)

    invitation_getter.assert_called_with(slug="foo")
    assert not login_m.called
    assert not users_m.create_user.called
    assert not users_m.create_remote_user.called
    assert not users_m.update_user.called


def test_authorize_wrong_nonce(id_token_generator, auth_req_generator):
    # The id token has a different nonce than the session
    id_token, claims = id_token_generator(nonce="a")
    request = auth_req_generator(id_token, nonce="b")
    with pytest.raises(JoseError):
        views.authorize(request)


def test_authorize_wrong_state(id_token_generator, auth_req_generator):
    # The incoming state query param is different from the session
    # This happens when the browser 'backwards' and 'forwards' buttons are used
    # we solve this by restarting the login process (keeping the success_url).
    id_token, claims = id_token_generator()
    request = auth_req_generator(id_token, state="a")
    request.session.pop("_state_oauth_a")
    request.session[LOGIN_REDIRECT_SESSION_KEY] = "/success"
    response = views.authorize(request)

    assert response.status_code == 302
    parsed_url = urlparse(response.url)
    parsed_params = parse_qs(parsed_url.query)
    assert parsed_url.path == "/login/"
    assert parsed_params == {REDIRECT_FIELD_NAME: ["/success"]}


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
    with pytest.raises(OAuthError, match="some_error"):
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
    request.session = {"_state_oauth_my_state": {"data": {"nonce": "x"}}}
    with pytest.raises(OAuthError, match="some_error: bla"):
        views.authorize(request)


def test_token_error_code_already_used(rq_mocker, rf, openid_configuration):
    # The incoming state query param is different from the session
    # This happens when the browser 'backwards' and 'forwards' buttons are used
    # we solve this by restarting the login process (keeping the success_url).
    rq_mocker.post(
        openid_configuration["token_endpoint"],
        status_code=400,
        json={"error": "invalid_grant", "error_description": "bla"},
    )
    # Create the request
    request = rf.get("http://testserver/authorize/?code=abc&state=my_state")
    request.session = {"_state_oauth_my_state": {"data": {"nonce": "bla"}}}
    response = views.authorize(request)

    assert response.status_code == 302
    parsed_url = urlparse(response.url)
    parsed_params = parse_qs(parsed_url.query)
    assert parsed_url.path == "/login/"
    assert parsed_params == {}
