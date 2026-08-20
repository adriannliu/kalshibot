import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from auth.credentials import Credentials, MissingCredentials, load_from_env
from auth.signer import RequestSigner, strip_query
from config import exchange


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return key.public_key(), pem


@pytest.fixture
def signer(keypair):
    _, pem = keypair
    return RequestSigner(Credentials(key_id="key-123", private_key_pem=pem, environment="demo"))


def test_query_parameters_are_excluded_from_the_signed_path():
    assert strip_query("/trade-api/v2/markets?status=open&limit=1000") == "/trade-api/v2/markets"


def test_signature_verifies_over_timestamp_method_and_path(keypair, signer):
    public_key, _ = keypair
    timestamp = 1750000000123
    path = "/trade-api/v2/markets"
    signature = base64.b64decode(signer.signature("GET", path + "?status=open", timestamp))

    public_key.verify(
        signature,
        ("%d%s%s" % (timestamp, "GET", path)).encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_headers_carry_millisecond_timestamp(signer):
    headers = signer.headers("GET", "/trade-api/ws/v2", 1750000000123)
    assert headers[exchange.HEADER_ACCESS_TIMESTAMP] == "1750000000123"
    assert headers[exchange.HEADER_ACCESS_KEY] == "key-123"
    assert len(headers[exchange.HEADER_ACCESS_TIMESTAMP]) == 13


def test_websocket_headers_sign_the_websocket_path(keypair, signer):
    public_key, _ = keypair
    headers = signer.ws_headers(1750000000123)
    public_key.verify(
        base64.b64decode(headers[exchange.HEADER_ACCESS_SIGNATURE]),
        ("1750000000123GET" + exchange.WS_SIGN_PATH).encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_rest_headers_prefix_the_api_version(keypair, signer):
    public_key, _ = keypair
    headers = signer.rest_headers("GET", "/markets", 1750000000123)
    public_key.verify(
        base64.b64decode(headers[exchange.HEADER_ACCESS_SIGNATURE]),
        ("1750000000123GET" + exchange.REST_SIGN_PREFIX + "/markets").encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_credentials_require_a_key_id():
    with pytest.raises(MissingCredentials):
        load_from_env({})


def test_credentials_require_a_private_key():
    with pytest.raises(MissingCredentials):
        load_from_env({"KALSHI_API_KEY_ID": "abc"})


def test_credentials_never_repr_the_private_key(keypair):
    _, pem = keypair
    creds = Credentials(key_id="key-123456789", private_key_pem=pem, environment="demo")
    assert "PRIVATE" not in repr(creds)
    assert "key-12" in repr(creds)


def test_environment_selects_endpoints(keypair):
    _, pem = keypair
    prod = Credentials(key_id="k", private_key_pem=pem, environment="prod")
    demo = Credentials(key_id="k", private_key_pem=pem, environment="demo")
    assert prod.ws_url == exchange.WS_URL_PROD
    assert demo.rest_base == exchange.REST_BASE_DEMO
