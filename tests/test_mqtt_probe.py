import datetime
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from custom_components.unifi_unas import config_flow
from custom_components.unifi_unas.config_flow import probe_mqtt


class FakeBroker:
    # answers the first CONNECT with a CONNACK return code, or never answers
    def __init__(self, return_code=0, reply=True, tls=None):
        self.return_code = return_code
        self.reply = reply
        self.tls = tls
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        try:
            if self.tls:
                conn = self.tls.wrap_socket(conn, server_side=True)
            conn.recv(1024)
            if self.reply:
                conn.sendall(bytes([0x20, 0x02, 0x00, self.return_code]))
            while conn.recv(1024):
                pass
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()
        self.thread.join(5)


@pytest.fixture
def broker(socket_enabled):
    brokers = []

    def start(**kwargs):
        brokers.append(FakeBroker(**kwargs))
        return brokers[-1]

    yield start
    for b in brokers:
        b.close()


@pytest.fixture(scope="module")
def tls_context(tmp_path_factory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "broker")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    path = tmp_path_factory.mktemp("tls")
    (path / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (path / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(path / "cert.pem", path / "key.pem")
    return context


def probe(port, use_tls=False, tls_insecure=False):
    return probe_mqtt("127.0.0.1", port, "unas", "secret", use_tls, tls_insecure)


def test_accepted(broker):
    assert probe(broker().port) is None


@pytest.mark.parametrize("return_code,expected", [
    (4, "mqtt_invalid_auth"),
    (5, "mqtt_invalid_auth"),
    (3, "mqtt_cannot_connect"),
])
def test_refused(broker, return_code, expected):
    assert probe(broker(return_code=return_code).port) == expected


def test_no_answer_times_out(broker, monkeypatch):
    monkeypatch.setattr(config_flow, "MQTT_PROBE_TIMEOUT", 0.5)
    assert probe(broker(reply=False).port) == "mqtt_timeout"


def test_nothing_listening(socket_enabled):
    sock = socket.create_server(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert probe(port) == "mqtt_cannot_connect"


def test_tls_self_signed(broker, tls_context):
    assert probe(broker(tls=tls_context).port, use_tls=True) == "mqtt_tls_verify_failed"
    assert probe(broker(tls=tls_context).port, use_tls=True, tls_insecure=True) is None
