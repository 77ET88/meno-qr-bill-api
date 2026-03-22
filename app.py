#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Response, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from qrbill import QRBill
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPDF, renderPM


# ========= Réglages injection =========
OFFSET_BELOW_PAYEE_BLOCK = 22
FALLBACK_CLEARANCE = 20
FONT_SIZE = 10
LINE_GAP = 12


# ========= Utils =========
def prettify_groups4(s: str) -> str:
    s = (s or "").replace(" ", "")
    return " ".join(s[i:i+4] for i in range(0, len(s), 4))


def _alnum_to_digits(s: str) -> str:
    out = []
    for ch in s:
        if ch.isdigit():
            out.append(ch)
        elif "A" <= ch <= "Z":
            out.append(str(ord(ch) - 55))
    return "".join(out)


def rf_from_base(base: str) -> str:
    base_compact = re.sub(r"[^0-9A-Z]", "", (base or "").upper())
    if not base_compact:
        raise ValueError("Base vide.")

    num = _alnum_to_digits(base_compact + "RF00")
    rem = 0
    for ch in num:
        rem = (rem * 10 + ord(ch) - 48) % 97
    check = 98 - rem
    return f"RF{check:02d}{base_compact}"


def build_kj_base(company_code, invoice_no, year=None, mt_prefix="MT00", client_code="KJ00"):
    if year is None:
        year = date.today().year

    cc = re.sub(r"\D", "", str(company_code))
    inv = re.sub(r"\D", "", str(invoice_no))

    if len(cc) != 4:
        raise ValueError("Code magasin invalide")
    if not inv:
        raise ValueError("Numéro facture invalide")

    return f"{mt_prefix}{year}{client_code}{cc}{inv}"


# ========= Génération PNG bas =========
def render_bottom_svg(bill, path):
    try:
        bill.as_svg(str(path), qr_only=True)
        return
    except TypeError:
        pass

    try:
        bill.as_svg(str(path), full_page=False)
        return
    except TypeError:
        pass

    bill.as_svg(str(path))


# ========= API =========
APP_NAME = "QR-Bill API"
API_KEY = os.getenv("QRBILL_API_KEY", "")

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GeneratePayload(BaseModel):
    amount: str = "162.15"
    iban: str = "CH15 0076 8300 1685 0780 5"
    lang: str = "fr"

    creditor_name: str = "Meno Transport"
    creditor_zip: str = "1785"
    creditor_city: str = "CRESSIER"
    creditor_street: str = ""
    creditor_house_no: str = ""

    debtor_name: str
    debtor_street: str
    debtor_zip: str
    debtor_city: str

    mt_prefix: str = "MT00"
    year: Optional[int] = None
    client_code: str = "KJ00"
    company_code: str
    invoice_no: str

    info_company: str = ""
    info_line1: str = ""
    info_line2: str = ""

    # 🔥 NOUVEAU
    output_format: str = "pdf"  # pdf ou png_bottom


def require_api_key(x_api_key):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/generate")
def generate(payload: GeneratePayload, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    try:
        base = build_kj_base(
            payload.company_code,
            payload.invoice_no,
            payload.year,
            payload.mt_prefix,
            payload.client_code
        )
        rf_reference = rf_from_base(base)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    creditor_street = f"{payload.creditor_street} {payload.creditor_house_no}".strip()

    bill = QRBill(
        account=payload.iban,
        creditor={
            "name": payload.creditor_name,
            "street": creditor_street,
            "pcode": payload.creditor_zip,
            "city": payload.creditor_city,
            "country": "CH",
        },
        amount=payload.amount,
        reference_number=rf_reference,
        debtor={
            "name": payload.debtor_name,
            "street": payload.debtor_street,
            "pcode": payload.debtor_zip,
            "city": payload.debtor_city,
            "country": "CH",
        },
        language=payload.lang,
    )

    tmp = Path("/tmp")

    # ===== PDF A4 =====
    if payload.output_format == "pdf":
        svg = tmp / "bill.svg"
        pdf = tmp / "bill.pdf"

        bill.as_svg(str(svg), full_page=True)

        drawing = svg2rlg(str(svg))
        renderPDF.drawToFile(drawing, str(pdf))

        return Response(
            content=pdf.read_bytes(),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="qr-bill.pdf"'}
        )

    # ===== PNG BAS =====
    svg = tmp / "bottom.svg"
    png = tmp / "bottom.png"

    render_bottom_svg(bill, svg)

    drawing = svg2rlg(str(svg))
    renderPM.drawToFile(drawing, str(png), fmt="PNG")

    return Response(
        content=png.read_bytes(),
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="qr-bottom.png"'}
    )
