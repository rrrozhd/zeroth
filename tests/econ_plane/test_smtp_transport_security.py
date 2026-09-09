"""Real STARTTLS regressions adapted from the independent package audit.

Only disposable certificates and loopback credentials; no external delivery.
"""

from contextlib import contextmanager
import socket
import ssl
import subprocess
import threading

import pytest
from pydantic import SecretStr
from zeroth.econ.plane import config
from zeroth.econ.plane.reports.mailer import (
    ConfiguredSmtpReportMailer,
    ReportEmail,
    ReportMailerUnavailable,
)


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    directory = tmp_path_factory.mktemp("smtp-certificate")
    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        capture_output=True,
        check=True,
    )
    return cert, key


@contextmanager
def smtp_server(certificate):
    cert, key = certificate
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert), str(key))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    observed = dict(auth=False, delivered=False, server_error=None)

    def serve():
        connection = None
        reader = None
        try:
            connection, _ = listener.accept()
            connection.settimeout(5)
            reader = connection.makefile("rb")
            connection.sendall(b"220 localhost test SMTP\r\n")
            while True:
                line = reader.readline()
                if not line:
                    break
                command = line.split(b" ", 1)[0].strip().upper()
                if command in (b"EHLO", b"HELO"):
                    connection.sendall(b"250-localhost\r\n250-STARTTLS\r\n250 AUTH PLAIN LOGIN\r\n")
                elif command == b"STARTTLS":
                    connection.sendall(b"220 Begin TLS\r\n")
                    reader.close()
                    reader = None
                    connection = tls.wrap_socket(connection, server_side=True)
                    reader = connection.makefile("rb")
                elif command == b"AUTH":
                    observed["auth"] = True  # Never retain the credential payload.
                    connection.sendall(b"235 Authenticated\r\n")
                elif command in (b"MAIL", b"RCPT", b"RSET"):
                    connection.sendall(b"250 OK\r\n")
                elif command == b"DATA":
                    connection.sendall(b"354 End with dot\r\n")
                    while True:
                        part = reader.readline()
                        if part == b".\r\n":
                            observed["delivered"] = True
                            break
                        if not part:
                            raise EOFError("message incomplete")
                    connection.sendall(b"250 Retained locally\r\n")
                elif command == b"QUIT":
                    connection.sendall(b"221 Bye\r\n")
                    break
                else:
                    connection.sendall(b"500 Unsupported\r\n")
        except Exception as exc:
            observed["server_error"] = type(exc).__name__
        finally:
            if reader is not None:
                reader.close()
            if connection is not None:
                connection.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], observed
    finally:
        listener.close()
        thread.join(6)
        assert not thread.is_alive(), "local SMTP fixture failed to terminate"


@pytest.mark.parametrize(
    "trust_certificate,host,accepted",
    [
        (False, "127.0.0.1", False),
        (True, "localhost", False),
        (True, "127.0.0.1", True),
    ],
    ids=["untrusted-certificate", "wrong-hostname", "trusted-matching-host"],
)
def test_real_starttls_authentication_boundary(
    certificate, monkeypatch, trust_certificate, host, accepted
):
    original_context = ssl.create_default_context
    if trust_certificate:
        # Add this disposable CA to the test trust store; preserve certificate and
        # hostname verification. The production mailer and smtplib stay real.
        def configured_context(*args, **kwargs):
            ctx = original_context(*args, **kwargs)
            ctx.load_verify_locations(cafile=str(certificate[0]))
            return ctx

        monkeypatch.setattr(ssl, "create_default_context", configured_context)
    with smtp_server(certificate) as (port, observed):
        for key, value in dict(
            report_email_enabled=True,
            report_email_from="sender@example.test",
            report_smtp_host=host,
            report_smtp_port=port,
            report_smtp_username="local-test-user",
            report_smtp_password=SecretStr("disposable-local-test-password"),
            report_smtp_starttls=True,
            report_smtp_timeout_seconds=3.0,
        ).items():
            monkeypatch.setattr(config.settings, key, value)
        error = None
        try:
            ConfiguredSmtpReportMailer().send(
                ReportEmail(
                    recipients=("local@example.test",),
                    subject="Local audit",
                    text_body="Synthetic fixture only",
                    attachment=b"local bytes",
                )
            )
        except Exception as exc:
            error = exc
    if accepted:
        assert error is None, repr(error)
        assert observed["auth"] and observed["delivered"], observed
    else:
        assert isinstance(error, ssl.SSLCertVerificationError), (type(error).__name__, observed)
        assert not observed["auth"] and not observed["delivered"], observed


def _plaintext_settings(monkeypatch):
    for key, value in dict(
        jwt_secret="synthetic-startup-test-key",
        report_email_enabled=True,
        report_email_from="test@example.com",
        report_smtp_host="localhost",
        report_smtp_username="test",
        report_smtp_starttls=False,
    ).items():
        monkeypatch.setattr(config.settings, key, value)


def test_startup_rejects_authenticated_plaintext_smtp(monkeypatch):
    _plaintext_settings(monkeypatch)
    with pytest.raises(config.EconConfigError, match="TLS"):
        config.validate_startup_settings()


def test_send_rejects_authenticated_plaintext_even_without_startup(monkeypatch):
    _plaintext_settings(monkeypatch)
    called = []

    def transport(*args, **kwargs):
        called.append(True)
        raise AssertionError("must reject before opening SMTP")

    monkeypatch.setattr("smtplib.SMTP", transport)
    with pytest.raises(ReportMailerUnavailable, match="TLS"):
        ConfiguredSmtpReportMailer().send(
            ReportEmail(recipients=("test@example.com",), subject="test", text_body="synthetic")
        )
    assert called == []


def test_disabled_email_does_not_require_transport_settings(monkeypatch):
    _plaintext_settings(monkeypatch)
    monkeypatch.setattr(config.settings, "report_email_enabled", False)
    config.validate_startup_settings()
