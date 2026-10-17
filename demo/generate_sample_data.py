"""Generate Leak's fully synthetic demo dataset.

    uv run python demo/generate_sample_data.py

Everything here is invented: the persona "Asha Testkumar", her accounts,
cards (test-range / masked numbers), PAN, phone and address. Output is
deterministic (seeded) and committed to demo/sample_data/.

Planted scenarios (what later phases should find):
  * Netflix ₹649 → ₹799 price hike in August (+ "price is changing" email)
  * Disney+ Hotstar ₹1 trial on 2 Jun → ₹299/month from 30 Jun
  * Hotstar is ALSO bundled in the Airtel Black plan (duplicate)
  * cult.fit cancelled on 15 Jun, ACH debits continue Jul-Sep (zombie)
  * Google One billed on both Axis UPI and SBI Card (duplicate)
  * ChatGPT Plus in USD + 3.5% forex markup + GST on markup (forex leakage)
  * Amazon Prime annual (renews May 2027), Spotify, YouTube Premium monthly
  * Swiggy One quarterly; SafeLife insurance annual premium
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pikepdf
from fpdf import FPDF

OUT = Path(__file__).parent / "sample_data"
LABELS_OUT = Path(__file__).parents[1] / "evals" / "fixtures" / "demo_labels.jsonl"

# Ground truth for evals: evidence_id -> expected records. Each record is
# {"kind": charge|renewal|price_change|cancellation, "merchant": <catalog id>,
#  "amount": number|None}. "charge" covers both transactions and receipts.
LABELS: list[dict[str, object]] = []


def label(evidence_id: str, *records: tuple[str, str, float | None]) -> None:
    LABELS.append(
        {
            "evidence_id": evidence_id,
            "records": [{"kind": k, "merchant": m, "amount": a} for k, m, a in records],
        }
    )


IST = timezone(timedelta(hours=5, minutes=30))
rng = random.Random(42)

NAME = "Asha Testkumar"
EMAIL = "asha.testkumar@example.com"
PHONE = "+91 98765 00001"
PAN = "ABCPT1234F"
ADDRESS = ["Flat 12B, Synthetic Heights", "4th Cross, Testnagar", "Bengaluru, Karnataka 560001"]
HDFC_ACCT = "50100123456789"
AXIS_ACCT = "918010012345678"
ICICI_CARD = "4315 XXXX XXXX 1007"
SBI_CARD = "XXXX XXXX XXXX 4444"
PASSWORDS = {"hdfc": "ASHA0101", "sbi": "01011990"}

MONTHS = [4, 5, 6, 7, 8, 9]
Y = 2026


def d(m: int, day: int) -> date:
    return date(Y, m, day)


def money(x: Decimal | float | int) -> str:
    q = Decimal(str(x)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    # Indian digit grouping: 1,23,456.78
    whole, frac = f"{q:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return f"{whole}.{frac}"


def ref() -> str:
    return str(rng.randint(600_000_000_000, 699_999_999_999))


@dataclass
class Txn:
    when: date
    desc: str
    amount: Decimal
    credit: bool = False
    fcur: str | None = None
    famt: Decimal | None = None


# --- ledgers -------------------------------------------------------------------


def hdfc_txns() -> list[Txn]:
    t: list[Txn] = []
    for m in MONTHS:
        t.append(
            Txn(
                d(m, 1),
                f"NEFT CR-TESTCORP SOLUTIONS PVT LTD-SALARY {m:02d}{Y}",
                Decimal(95000),
                credit=True,
            )
        )
        t.append(
            Txn(d(m, 3), f"UPI-RAMESH LANDLORD-ramesh.ll@okicici-{ref()}-RENT", Decimal(28000))
        )
        price = 649 if m <= 7 else 799
        t.append(Txn(d(m, 5), f"UPI-NETFLIX-netflix@hdfcbank-{ref()}-AUTOPAY", Decimal(price)))
        t.append(Txn(d(m, 9), f"BILLPAY-BESCOM-{ref()}", Decimal(rng.randint(900, 1900))))
        t.append(Txn(d(m, 14), "NWD-416021XXXXXX6789-HDFC ATM KORAMANGALA", Decimal(2000)))
        for day in rng.sample(range(10, 28), 3):
            t.append(
                Txn(
                    d(m, day),
                    f"UPI-SWIGGY-swiggy@ybl-{ref()}-ORDER",
                    Decimal(rng.randint(180, 750)),
                )
            )
        if m in (4, 7):
            t.append(
                Txn(d(m, 7), f"UPI-SWIGGY ONE-swiggyone@axisbank-{ref()}-MEMBERSHIP", Decimal(299))
            )
    return sorted(t, key=lambda x: x.when)


def axis_txns() -> list[Txn]:
    t: list[Txn] = []
    for m in MONTHS:
        t.append(Txn(d(m, 2), f"IMPS/P2A/{ref()}/SELF TRANSFER", Decimal(20000), credit=True))
        t.append(Txn(d(m, 10), f"UPI/P2M/{ref()}/GOOGLE INDIA DIGITAL/GOOGLE ONE", Decimal(130)))
        t.append(Txn(d(m, 12), "ACH-DR/CULTFIT HEALTHCARE PVT LTD/CULTPASS ELITE", Decimal(1499)))
        t.append(
            Txn(
                d(m, 20),
                f"UPI/P2P/{ref()}/9876500001@ybl/MOBILE RECHARGE",
                Decimal(rng.choice([239, 299])),
            )
        )
    t.append(Txn(d(8, 20), "SAFELIFE INSURANCE CO LTD/ANNUAL PREMIUM", Decimal(12000)))
    return sorted(t, key=lambda x: x.when)


USD_INR = {4: "86.92", 5: "87.15", 6: "87.40", 7: "87.66", 8: "87.81", 9: "88.04"}


def icici_txns() -> list[Txn]:
    t: list[Txn] = []
    for m in MONTHS:
        inr = (Decimal(20) * Decimal(USD_INR[m])).quantize(Decimal("0.01"))
        markup = (inr * Decimal("0.035")).quantize(Decimal("0.01"))
        gst = (markup * Decimal("0.18")).quantize(Decimal("0.01"))
        t.append(
            Txn(
                d(m, 15),
                "OPENAI *CHATGPT SUBSCR SAN FRANCISCO US",
                inr,
                fcur="USD",
                famt=Decimal(20),
            )
        )
        t.append(Txn(d(m, 15), "MARKUP FEE ON FOREIGN CURRENCY TXN", markup))
        t.append(Txn(d(m, 15), "IGST ON MARKUP FEE @18%", gst))
        t.append(Txn(d(m, 18), "GOOGLE *YOUTUBEPREMIUM MUMBAI IN", Decimal(149)))
        t.append(Txn(d(m, 25), "PAYMENT RECEIVED - THANK YOU", Decimal(8000), credit=True))
        for day in rng.sample(range(3, 27), 2):
            t.append(
                Txn(d(m, day), "AMAZON PAY INDIA BANGALORE IN", Decimal(rng.randint(300, 2500)))
            )
    t.append(Txn(d(6, 2), "DISNEY PLUS HOTSTAR TRIAL MUMBAI IN", Decimal(1)))
    for m, day in ((6, 30), (7, 30), (8, 30), (9, 29)):
        t.append(Txn(d(m, day), "DISNEY PLUS HOTSTAR MUMBAI IN", Decimal(299)))
    return sorted(t, key=lambda x: x.when)


def sbi_txns() -> list[Txn]:
    t: list[Txn] = []
    for m in MONTHS:
        t.append(Txn(d(m, 12), "SPOTIFY INDIA MUMBAI", Decimal(119)))
        t.append(Txn(d(m, 22), "GOOGLE *GOOGLE ONE MUMBAI", Decimal(130)))
        t.append(Txn(d(m, 26), "PAYMENT RECEIVED BBPS", Decimal(3000), credit=True))
        for day in rng.sample(range(2, 27), 2):
            t.append(
                Txn(
                    d(m, day),
                    rng.choice(["ZOMATO GURGAON", "UBER INDIA BANGALORE"]),
                    Decimal(rng.randint(150, 900)),
                )
            )
    t.append(Txn(d(5, 20), "AMAZON PRIME MEMBERSHIP BANGALORE", Decimal(1499)))
    return sorted(t, key=lambda x: x.when)


# --- PDF rendering -----------------------------------------------------------------


def write_pdf(
    path: Path,
    header: list[str],
    col_header: str,
    rows: list[str],
    footer: list[str],
    password: str | None,
) -> None:
    pdf = FPDF(format="A4")
    pdf.set_creation_date(datetime(2026, 10, 1, tzinfo=IST))  # byte-stable output
    pdf.set_auto_page_break(False)
    per_page = 40
    chunks = [rows[i : i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    for i, chunk in enumerate(chunks):
        pdf.add_page()
        pdf.set_font("Courier", size=7)
        lines = (header if i == 0 else [header[0]]) + ["", col_header] + chunk
        if i == len(chunks) - 1:
            lines += ["", *footer]
        lines.append(f"Page {i + 1} of {len(chunks)}")
        for ln in lines:
            pdf.cell(0, 4, ln, new_x="LMARGIN", new_y="NEXT")
    tmp = path.with_suffix(".tmp.pdf")
    pdf.output(str(tmp))
    with pikepdf.open(tmp) as p:
        if password:
            # AES salts are random, so encrypted files differ byte-wise run to run;
            # Leak derives evidence ids from the extracted text, so ids stay stable.
            p.save(path, encryption=pikepdf.Encryption(user=password, owner=password + "-own", R=6))
        else:
            p.save(path, deterministic_id=True)
    tmp.unlink()


def person_block() -> list[str]:
    return [NAME.upper(), *ADDRESS, f"Registered Mobile: {PHONE}", f"Email: {EMAIL}"]


def gen_hdfc() -> None:
    bal = Decimal(125000)
    rows = []
    for t in hdfc_txns():
        bal += t.amount if t.credit else -t.amount
        dt = t.when.strftime("%d/%m/%y")
        rows.append(
            f"{dt}  {t.desc:<58} {('0000' + ref()):>18}  {dt}  "
            f"{money(t.amount):>12}  {money(bal):>14}"
        )
    header = [
        "HDFC BANK LTD",
        "Statement of account",
        "",
        *person_block(),
        f"Account No : {HDFC_ACCT}   Account Type: SAVINGS   Cust ID : 44120087",
        "Statement From : 01/04/2026  To : 30/09/2026",
        f"Opening Balance : {money(125000)}",
    ]
    write_pdf(
        OUT / "statements" / "hdfc_savings_apr-sep_2026.pdf",
        header,
        "Date      Narration                                                  "
        "Chq./Ref.No.        Value Dt   Amount        Closing Balance",
        rows,
        [f"Closing Balance : {money(bal)}", "*** End of Statement ***"],
        PASSWORDS["hdfc"],
    )


def gen_axis() -> None:
    bal = Decimal(53000)
    rows = []
    for t in axis_txns():
        bal += t.amount if t.credit else -t.amount
        rows.append(
            f"{t.when.strftime('%d-%m-%Y')}  {t.desc:<60} {money(t.amount):>12}  "
            f"{money(bal):>14}  1471"
        )
    header = [
        "AXIS BANK",
        f"Statement of Account No :{AXIS_ACCT} for the period (From : 01-04-2026 To : 30-09-2026)",
        "",
        *person_block(),
        f"PAN : {PAN}",
        f"OPENING BALANCE {money(53000)}",
    ]
    write_pdf(
        OUT / "statements" / "axis_savings_apr-sep_2026.pdf",
        header,
        "Tran Date   Particulars                                                  "
        "Amount        Balance          Init.Br",
        rows,
        [
            f"CLOSING BALANCE {money(bal)}",
            "Unless the constituent notifies the bank immediately of any discrepancy...",
        ],
        None,
    )


def gen_icici() -> None:
    rows = []
    for t in icici_txns():
        intl = f"{t.fcur} {money(t.famt or 0)}" if t.fcur else ""
        pts = 0 if t.credit or t.fcur else int(t.amount // 100)
        rows.append(
            f"{t.when.strftime('%d/%m/%Y')}  {rng.randint(10**10, 10**11 - 1)}  "
            f"{t.desc:<42} {intl:>12}  {pts:>4}  {money(t.amount):>10}"
            + (" CR" if t.credit else "")
        )
    header = [
        "ICICI Bank",
        "Credit Card Statement",
        "",
        *person_block(),
        f"Card Number : {ICICI_CARD}   Card Type: ICICI Bank Coral Visa",
        "Statement Period : 01/04/2026 - 30/09/2026",
    ]
    write_pdf(
        OUT / "statements" / "icici_credit_card_apr-sep_2026.pdf",
        header,
        "Date        SerNo.       Transaction Details                          "
        "Intl.Amount  Pts   Amount (Rs)",
        rows,
        ["Total Amount Due : see monthly statement", "Minimum Amount Due : -"],
        None,
    )


def gen_sbi() -> None:
    rows = [
        f"{t.when.strftime('%d %b %y')}  {t.desc:<48} {money(t.amount):>10} "
        + ("C" if t.credit else "D")
        for t in sbi_txns()
    ]
    header = [
        "SBI Card",
        "Monthly Statement (Consolidated Apr - Sep 2026)",
        "",
        *person_block(),
        f"Credit Card Number : {SBI_CARD}",
        "Card: SimplyCLICK SBI Card",
    ]
    write_pdf(
        OUT / "statements" / "sbi_card_apr-sep_2026.pdf",
        header,
        "Date       Transaction Details                                Amount (Rs)",
        rows,
        ["Total Outstanding : see monthly statement"],
        PASSWORDS["sbi"],
    )


# --- emails ------------------------------------------------------------------------

_eml_n = 0


def eml(
    when: datetime,
    sender: str,
    subject: str,
    text: str,
    html: str | None = None,
    *,
    expect: tuple[tuple[str, str, float | None], ...] = (),
) -> None:
    global _eml_n
    _eml_n += 1
    label(f"gmail:demo-{_eml_n:04d}", *expect)
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = f"{NAME} <{EMAIL}>"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = f"<demo-{_eml_n:04d}@leak.demo>"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
        msg.set_boundary(f"leak-demo-boundary-{_eml_n:04d}")
    (OUT / "emails" / f"demo-{_eml_n:04d}.eml").write_bytes(bytes(msg))


def at(m: int, day: int, hour: int = 9) -> datetime:
    return datetime(Y, m, day, hour, rng.randint(0, 59), tzinfo=IST)


def gen_emails() -> None:
    for m in MONTHS:
        price = 649 if m <= 7 else 799
        eml(
            at(m, 5),
            "Netflix <info@account.netflix.com>",
            "Your Netflix payment receipt",
            f"Hi {NAME.split()[0]},\n\nWe received your payment of Rs {price}.00 for Netflix "
            f"Premium.\nPayment method: UPI AutoPay\nBilling date: {d(m, 5):%d %B %Y}\n"
            f"Next billing date: {d(m % 12 + 1, 5) if m < 12 else d(1, 5):%d %B %Y}\n\n"
            "Manage your membership at netflix.com/account.\n",
            f"<html><body><h2>Payment receipt</h2><p>Hi {NAME.split()[0]},</p>"
            f"<table><tr><td>Plan</td><td>Premium</td></tr><tr><td>Amount</td>"
            f"<td>&#8377;{price}.00</td></tr><tr><td>Payment</td><td>UPI AutoPay</td></tr>"
            "</table></body></html>",
            expect=(("charge", "netflix", price),),
        )
    eml(
        at(7, 10),
        "Netflix <info@account.netflix.com>",
        "An update to your Netflix price",
        f"Hi {NAME.split()[0]},\n\nStarting with your billing date on 5 August 2026, your "
        "Premium plan price is changing from Rs 649/month to Rs 799/month.\n"
        "No action is needed to keep watching. You can change or cancel your plan anytime.\n",
        expect=(("price_change", "netflix", 799),),
    )
    for m in (4, 7):
        eml(
            at(m, 12),
            "Spotify <no-reply@spotify.com>",
            "Your Spotify Premium receipt",
            "Spotify Premium Individual\nRs 119.00 charged to your SBI card ending 4444.\n"
            f"Billed on {d(m, 12):%d %b %Y}. Renews monthly.\n",
            expect=(("charge", "spotify", 119),),
        )
    eml(
        at(5, 20),
        "Amazon.in <auto-confirm@amazon.in>",
        "Welcome to Amazon Prime",
        f"Hello {NAME},\n\nThank you for joining Prime. Annual membership: Rs 1,499.00\n"
        "Your membership renews on 20 May 2027.\n\nShipping Address:\n"
        + "\n".join(ADDRESS)
        + f"\nPhone: {PHONE}\n",
        expect=(("charge", "amazon_prime", 1499),),
    )
    eml(
        at(6, 2),
        "Disney+ Hotstar <noreply@hotstar.com>",
        "Your trial has started",
        "Your Disney+ Hotstar Super trial is active. We charged Rs 1 to verify your card "
        "XX1007.\nAfter the trial ends on 30 Jun 2026, you'll be charged Rs 299/month "
        "unless you cancel.\n",
        expect=(("charge", "disney_hotstar", 1), ("renewal", "disney_hotstar", 299)),
    )
    eml(
        at(7, 3),
        "Airtel <ebill@airtel.com>",
        "Your Airtel Black bill for June",
        f"Dear {NAME},\nAirtel Black plan Rs 1,099/month. Your plan includes Disney+ Hotstar "
        "Mobile, Amazon Prime (via Airtel Thanks) and Airtel Xstream Play at no extra cost.\n"
        f"Registered mobile: {PHONE}\nAccount No: 1203455512\n",
        expect=(("charge", "airtel_black", 1099),),
    )
    eml(
        at(6, 15, 20),
        "cult.fit <support@cult.fit>",
        "Your cultpass ELITE has been cancelled",
        f"Hi {NAME.split()[0]},\n\nAs requested, your cultpass ELITE membership has been "
        "cancelled effective 15 Jun 2026. Your auto-debit mandate will be revoked and you "
        "will not be charged again.\n\nCancellation reference: CF-CXL-88213\n",
        expect=(("cancellation", "cultfit", None),),
    )
    for m in (5, 8):
        inr = (Decimal(20) * Decimal(USD_INR[m])).quantize(Decimal("0.01"))
        eml(
            at(m, 15, 6),
            "OpenAI <noreply@tm.openai.com>",
            "Your ChatGPT Plus receipt",
            f"Receipt\nChatGPT Plus subscription\nAmount paid: $20.00 USD\n"
            f"Paid with Visa ending 1007 on {d(m, 15):%B %d, %Y}.\n"
            f"(Your bank may convert this to about Rs {inr} plus foreign transaction fees.)\n",
            expect=(("charge", "chatgpt", 20),),
        )
    eml(
        at(4, 10),
        "Google Payments <payments-noreply@google.com>",
        "Your Google One receipt",
        f"Google One 100 GB — Rs 130.00/month\nPaid via UPI.\nAccount: {EMAIL}\n",
        expect=(("charge", "google_one", 130),),
    )
    eml(
        at(4, 22),
        "Google Payments <payments-noreply@google.com>",
        "Your Google One receipt",
        "Google One 100 GB — Rs 130.00/month\nPaid with SBI card ending 4444.\n"
        "Account: asha.family.test@example.com\n",
        expect=(("charge", "google_one", 130),),
    )
    eml(
        at(6, 18),
        "YouTube <noreply@youtube.com>",
        "Your YouTube Premium receipt",
        "YouTube Premium Individual — Rs 149.00/month, charged to card XX1007.\n",
        expect=(("charge", "youtube_premium", 149),),
    )
    eml(
        at(8, 1),
        "SafeLife Insurance <renewals@safelife-insurance.example>",
        "Policy renewal notice — premium due 20 Aug 2026",
        f"Dear {NAME},\nPolicy No: SL/HLT/2025/0042117\nPolicy holder PAN: {PAN}\n"
        "Annual premium of Rs 12,000.00 will be auto-debited from Axis Bank A/c "
        f"{AXIS_ACCT} on 20 Aug 2026.\nCommunication Address: " + ", ".join(ADDRESS) + "\n",
        expect=(("renewal", "safelife_insurance", 12000),),
    )
    eml(
        at(7, 7),
        "Swiggy <no-reply@swiggy.in>",
        "Swiggy One renewed",
        "Your Swiggy One membership has been renewed for 3 months at Rs 299.\n",
        expect=(("charge", "swiggy_one", 299),),
    )
    eml(
        at(5, 2),
        "The Weekly Byte <news@weeklybyte.example>",
        "This week in tech",
        "Ten links we liked this week. Unsubscribe anytime.\n",
    )
    eml(
        at(6, 21),
        "Swiggy <no-reply@swiggy.in>",
        "Order delivered",
        "Your order from Meghana Foods was delivered. Total Rs 486.\n",
        expect=(("charge", "swiggy", 486),),
    )


# --- SMS ---------------------------------------------------------------------------


def gen_sms() -> None:
    rows: list[tuple[str, datetime, str]] = []
    expect: dict[int, tuple[tuple[str, str, float | None], ...]] = {}

    def last(*records: tuple[str, str, float | None]) -> None:
        expect[len(rows) - 1] = records

    for m in MONTHS:
        price = 649 if m <= 7 else 799
        rows.append(
            (
                "AX-HDFCBK",
                at(m, 5, 8),
                f"Rs.{price}.00 debited from a/c **6789 on {d(m, 5):%d-%m-%y} to VPA "
                f"netflix@hdfcbank (UPI Ref No {ref()}). Not you? Call 18002586161",
            )
        )
        last(("charge", "netflix", price))
        rows.append(
            (
                "VM-SBICRD-S",
                at(m, 12, 10),
                f"Rs.119.00 spent on your SBI Credit Card ending 4444 at SPOTIFY INDIA on "
                f"{d(m, 12):%d/%m/%y}. Trxn. not done by you? Report at sbicard.com/Dispute",
            )
        )
        last(("charge", "spotify", 119))
        rows.append(
            (
                "JD-AXISBK",
                at(m, 12, 7),
                f"ACH Debit of INR 1499.00 from A/c no. XX5678 towards CULTFIT HEALTHCARE "
                f"on {d(m, 12):%d-%m-%Y}. Avl Bal INR {rng.randint(30000, 60000)}.00",
            )
        )
        last(("charge", "cultfit", 1499))
    rows.append(
        (
            "VK-ICICIB",
            at(6, 2, 21),
            "INR 1.00 spent on ICICI Bank Card XX1007 on 02-Jun-26 on HOTSTAR. "
            "Avl Limit: INR 1,48,220.00. If not you, call 1800 2662.",
        )
    )
    last(("charge", "disney_hotstar", 1))
    rows.append(
        (
            "VK-ICICIB",
            at(6, 30, 6),
            "INR 299.00 spent on ICICI Bank Card XX1007 on 30-Jun-26 on HOTSTAR. "
            "Avl Limit: INR 1,45,103.00.",
        )
    )
    last(("charge", "disney_hotstar", 299))
    rows.append(
        (
            "AX-HDFCBK",
            at(4, 1, 9),
            "UPI-Mandate for NETFLIX of Rs 649.00 (monthly) created on a/c **6789. "
            f"Registered mobile {PHONE}.",
        )
    )
    last()  # mandate set-up: no transaction
    rows.append(
        (
            "AD-HDFCBK",
            at(5, 3, 11),
            "OTP for txn of Rs 2,340.00 at AMAZON is 482913. Valid 10 min. Do not share.",
        )
    )
    rows.append(
        ("+919812300002", at(5, 4, 19), "Hey Asha, dinner on Friday? Bring the board game!")
    )
    rows.append(("VM-SWIGGY", at(6, 1, 12), "Craving biryani? Flat 50% off tonight. T&C"))

    for i, records in expect.items():
        sender, when, body = rows[i]
        ms = int(when.timestamp() * 1000)
        digest = hashlib.sha256(f"{sender.upper()}|{ms}|{body}".encode()).hexdigest()
        label(f"sms:{digest[:12]}", *records)

    out = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>',
        f'<smses count="{len(rows)}" backup_set="demo" backup_date="1790000000000">',
    ]
    for sender, when, body in sorted(rows, key=lambda r: r[1]):
        ms = int(when.timestamp() * 1000)
        out.append(
            f'  <sms protocol="0" address={quoteattr(sender)} date="{ms}" type="1" '
            f'subject="null" body={quoteattr(body)} toa="null" sc_toa="null" '
            f'read="1" status="-1" locked="0" date_sent="{ms}" '
            f"readable_date={quoteattr(when.strftime('%d %b %Y %H:%M:%S'))} "
            'contact_name="(Unknown)" />'
        )
    out.append("</smses>")
    (OUT / "sms" / "sms-backup-demo.xml").write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    for sub in ("emails", "statements", "sms"):
        (OUT / sub).mkdir(parents=True, exist_ok=True)
        for f in (OUT / sub).iterdir():
            f.unlink()
    gen_hdfc()
    gen_axis()
    gen_icici()
    gen_sbi()
    gen_emails()
    gen_sms()
    LABELS_OUT.parent.mkdir(parents=True, exist_ok=True)
    LABELS_OUT.write_text("".join(json.dumps(x) + "\n" for x in LABELS))
    print(f"wrote demo data to {OUT} and {len(LABELS)} labels to {LABELS_OUT}")


if __name__ == "__main__":
    main()
