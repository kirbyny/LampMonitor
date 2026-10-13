import asyncio

import pytest

from lampmonitor import notifier as notifier_module
from lampmonitor.notifier import EmailNotifier, NotifierError, build_message

from .conftest import mail_settings


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.calls = []
        self.messages = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.messages.append(message)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notifier_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notifier_module.smtplib, "SMTP_SSL", FakeSMTP)


def test_build_message_with_attachment():
    message = build_message(
        mail_settings(email_from="LampMonitor <lm@example.com>"), ["a@example.com", "b@example.com"],
        "OFF: Circuit 12", "body text", b"\xff\xd8jpeg",
    )  # fmt: skip
    assert message["Subject"] == "[LampMonitor] OFF: Circuit 12"
    assert message["To"] == "a@example.com, b@example.com"
    assert message["From"] == "LampMonitor <lm@example.com>"
    assert message["Message-ID"].endswith("@example.com>")
    attachments = list(message.iter_attachments())
    assert [a.get_filename() for a in attachments] == ["snapshot.jpg"]


def test_send_uses_starttls_and_login():
    settings = mail_settings(smtp_port=587, smtp_username="user", smtp_password="pw")
    asyncio.run(EmailNotifier().send(settings, ["ops@example.com"], "Subject", "Body"))
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.calls == ["starttls", ("login", "user", "pw")]
    assert smtp.messages[0]["To"] == "ops@example.com"


def test_send_without_security_or_login():
    settings = mail_settings(smtp_security="none", smtp_port=25)
    asyncio.run(EmailNotifier().send(settings, ["ops@example.com"], "Subject", "Body"))
    assert FakeSMTP.instances[0].calls == []


@pytest.mark.parametrize(
    ("settings", "recipients", "match"),
    [
        (mail_settings(smtp_host=""), ["ops@example.com"], "no SMTP server"),
        (mail_settings(), [], "no recipients"),
        (mail_settings(email_from=""), ["ops@example.com"], "no From address"),
    ],
)
def test_send_explains_configuration_problems(settings, recipients, match):
    with pytest.raises(NotifierError, match=match):
        asyncio.run(EmailNotifier().send(settings, recipients, "Subject", "Body"))
