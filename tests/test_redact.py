"""Secret-shaped strings never reach the judge or the ledger. Test tokens are assembled at runtime
so this file does not itself look like it contains credentials."""

from ai_incidents.redact import MASK, Redactor

r = Redactor()


def test_token_formats():
    samples = [
        "gh" + "p_" + "A" * 36,
        "github" + "_pat_" + "B" * 30,
        "s" + "k-ant-" + "c" * 40,
        "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "xo" + "xb-" + "1234567890-abcdef",
        "ey" + "J" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
    ]
    for s in samples:
        out = r(f"token is {s} here")
        assert s not in out and MASK in out, s


def test_private_key_block():
    pem = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----"
    assert r("key:\n" + pem + "\ndone") == "key:\n" + MASK + "\ndone"


def test_assignments_keep_the_key_and_drop_the_value():
    assert r("db_password=s3cr3t-value") == f"db_password={MASK}"
    assert r('"api_key": "abcdef123456"') == f'"api_key": "{MASK}'  + '"'
    assert r("SMTP_PASS" + "WORD: hunter2hunter2") == f"SMTP_PASSWORD: {MASK}"


def test_placeholders_and_numbers_are_left_alone():
    for s in ["password=$DB_PASSWORD", "token: ${TOKEN}", "input_tokens: 123456", "password=********",
              "secret: true"]:
        assert r(s) == s, s


def test_bearer_and_url_credentials():
    assert r("Authorization: Bearer " + "x" * 30) == f"Authorization: Bearer {MASK}"
    assert r("postgres://app:" + "pw123456" + "@db.example.com/x") == f"postgres://app:{MASK}@db.example.com/x"


def test_ordinary_prose_untouched():
    s = "The author reverted the password-reset flow; tokens were fine. See docs at https://example.com/a:b"
    assert r(s) == s


def test_disabled_and_extra_patterns():
    assert Redactor(enabled=False)("password=abcdefgh") == "password=abcdefgh"
    assert Redactor([r"INTERNAL-\d{6}"])("id INTERNAL-123456") == f"id {MASK}"
