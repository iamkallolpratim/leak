import pytest

from leak.privacy.redact import (
    PrivacyProfile,
    Redactor,
    Vault,
    find_pii,
    luhn_ok,
    verhoeff_ok,
)

PROFILE = PrivacyProfile(
    names=["Asha Testkumar"],
    emails=["asha.testkumar@example.com"],
    address_fragments=["Synthetic Heights"],
)


@pytest.fixture
def r(tmp_path):
    return Redactor(Vault(tmp_path / "vault.db"), PROFILE)


def kinds(text):
    return [s.kind for s in find_pii(text, PROFILE)]


@pytest.mark.parametrize(
    "text,kind",
    [
        ("card 4111 1111 1111 1111 used", "CARD"),
        ("card 5555555555554444", "CARD"),
        ("Card XX1007 spent", "CARD"),
        ("4315 XXXX XXXX 1007", "CARD"),
        ("Visa ending 1007", "CARD"),
        ("PAN: ABCPT1234F", "PAN"),
        ("Aadhaar 2345 6789 0124", "AADHAAR"),
        ("Aadhaar No: 234567890124", "AADHAAR"),
        ("debited from a/c **6789", "ACCT"),
        ("Account No : 50100123456789", "ACCT"),
        ("call +91 98765 00001", "PHONE"),
        ("call 9876500001 now", "PHONE"),
        ("mail asha.testkumar@example.com", "EMAIL"),
        ("from friend.person@gmail.com", "EMAIL"),
        ("paid to ramesh.ll@okicici", "UPI"),
        ("paid to 9876500001@ybl", "UPI"),
        ("UPI Ref 612345678901", "NUM"),
        ("Dear Asha, hello", "NAME"),
        ("Shipping Address: 1 Some Road, Pune", "ADDRESS"),
        ("Bengaluru, Karnataka 560001", "ADDRESS"),
        ("Flat 12B, Synthetic Heights", "ADDRESS"),
    ],
)
def test_detects(text, kind):
    assert kind in kinds(text)


@pytest.mark.parametrize(
    "text",
    [
        "From: Netflix <info@account.netflix.com>",
        "no-reply@spotify.com",
        "auto-confirm@amazon.in",
        "UPI-NETFLIX-netflix@hdfcbank-AUTOPAY",  # merchant VPA kept
        "Amount 1,23,456.78 on 05/04/2026",
        "Rs 649.00 for 3 months",
        "15/04/2026 OPENAI *CHATGPT USD 20.00",
    ],
)
def test_leaves_non_pii_alone(text):
    assert kinds(text) == []


def test_checksums():
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")
    assert verhoeff_ok("234567890124") and not verhoeff_ok("234567890125")


def test_luhn_invalid_long_number_is_not_a_card():
    assert "CARD" not in kinds("ref 4111111111111112")


def test_same_value_same_token_and_unmask(r):
    a = r.redact("card 4315 XXXX XXXX 1007 and Card XX1007 and Visa ending 1007")
    assert a.count("[CARD_1]") == 3
    out = r.redact("Call +91 98765 00001 or 9876500001")
    assert out == "Call [PHONE_1] or [PHONE_1]"
    assert r.vault.unmask(out) == "Call +91 98765 00001 or +91 98765 00001"


def test_idempotent_and_clean(r):
    text = (
        "Dear Asha Testkumar, PAN ABCPT1234F, a/c 50100123456789, "
        "asha.testkumar@example.com, +91 98765 00001\nBilling Address:\nFlat 1\nPune 411001\n"
    )
    once = r.redact(text)
    assert r.leaks(once) == []
    assert r.redact(once) == once


def test_line_count_preserved(r):
    text = "line1\nBilling Address:\nFlat 1\nSome Road\n\nline6 a/c 50100123456789"
    out = r.redact(text)
    assert len(out.splitlines()) == len(text.splitlines())
    assert out.splitlines()[5].startswith("line6")


def test_vault_never_stores_plain_index(r, tmp_path):
    r.redact("PAN ABCPT1234F")
    row = next(iter(r.vault.db.query("select digest from entries")))
    assert "ABCPT1234F" not in row["digest"]
