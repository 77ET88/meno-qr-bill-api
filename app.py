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
from reportlab.graphics import renderPDF
from reportlab.pdfgen import canvas


# ========= Réglages injection (anti-superposition) =========
OFFSET_BELOW_PAYEE_BLOCK = 22
FALLBACK_CLEARANCE = 20
FONT_SIZE = 10
LINE_GAP = 12
SHIFT_X_LEFT = 0
SHIFT_X_RIGHT = 0


# ========= Utils ISO11649 =========
def prettify_groups4(s: str) -> str:
    s = (s or "").replace(" ", "")
    return " ".join(s[i:i+4] for i in range(0, len(s), 4))


def _alnum_to_digits(s: str) -> str:
    out = []
    for ch in s:
        if ch.isdigit():
            out.append(ch)
        elif "A" <= ch <= "Z":
            out.append(str(ord(ch) - 55))  # A=10 ... Z=35
    return "".join(out)


def rf_from_base(base: str) -> str:
    base_compact = re.sub(r"[^0-9A-Z]", "", (base or "").upper())
    if not base_compact:
        raise ValueError("Base vide.")
    if len(base_compact) > 21:
        raise ValueError(f"Base ISO11649 trop longue (>21): {base_compact} (len={len(base_compact)})")

    num = _alnum_to_digits(base_compact + "RF00")
    rem = 0
    for ch in num:
        rem = (rem * 10 + ord(ch) - 48) % 97
    check = 98 - rem
    return f"RF{check:02d}{base_compact}"


def _clean_alnum_upper(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


def build_kj_base(
    company_code: str,
    invoice_no: str,
    year: Optional[int] = None,
    mt_prefix: str = "MT00",
    client_code: str = "KJ00",
) -> str:
    """
    BASE ISO11649 (sans RFxx) format :
      MT00 + YYYY + KJ00 + CCCC + INVOICE
    Exemple: MT002026KJ0009601929
    """
    if year is None:
        year = date.today().year

    cc = re.sub(r"\D", "", str(company_code or ""))
    inv = re.sub(r"\D", "", str(invoice_no or ""))

    if len(cc) != 4:
        raise ValueError("Code magasin invalide (4 chiffres requis, ex: 0960).")
    if not inv:
        raise ValueError("Numéro de facture invalide (chiffres requis).")
    if len(inv) > 5:
        raise ValueError("Numéro de facture trop long (max 5 chiffres ISO11649).")

    base = f"{_clean_alnum_upper(mt_prefix)}{int(year):04d}{_clean_alnum_upper(client_code)}{cc}{inv}"
    base = _clean_alnum_upper(base)
    if len(base) > 21:
        raise ValueError("Base ISO11649 > 21 caractères (non conforme).")
    return base


# ========= Injection "Informations complémentaires" (anti chevauchement) =========
def inject_info_both_sides(svg_path: Path, printed_ref: str, lines):
    if not lines or not any(lines):
        return

    NS = {"svg": "http://www.w3.org/2000/svg"}
    ET.register_namespace("", NS["svg"])
    tree = ET.parse(str(svg_path))
    root = tree.getroot()

    refs = []
    for parent in root.iter():
        for node in list(parent):
            if node.tag.endswith("text") and "".join(node.itertext()).strip() == printed_ref:
                try:
                    x = float(node.attrib.get("x", "0"))
                    y = float(node.attrib.get("y", "0"))
                    refs.append((x, y, parent))
                except ValueError:
                    pass

    if not refs:
        for parent in root.iter():
            for node in list(parent):
                if node.tag.endswith("text") and "".join(node.itertext()).strip() == "Référence":
                    try:
                        x = float(node.attrib.get("x", "0"))
                        y = float(node.attrib.get("y", "0")) + 12
                        refs.append((x, y, parent))
                    except ValueError:
                        pass

    if not refs:
        return

    left, right = min(refs, key=lambda t: t[0]), max(refs, key=lambda t: t[0])
    sides = [("left", left, SHIFT_X_LEFT), ("right", right, SHIFT_X_RIGHT)]
    if left == right:
        sides = [("right", right, SHIFT_X_RIGHT)]

    n_lines = 1 + sum(1 for s in (lines[:3]) if s)

    def block_h():
        return FONT_SIZE + (n_lines - 1) * LINE_GAP

    def inject_for_side(x_ref, y_ref, parent, x_shift):
        # delete previous injected lines near this column
        to_del = []
        for n in list(parent):
            if not n.tag.endswith("text"):
                continue
            txt = "".join(n.itertext()).strip()
            if txt and (txt == "Informations complémentaires" or txt in lines):
                try:
                    x = float(n.attrib.get("x", "0"))
                except ValueError:
                    continue
                if abs(x - x_ref) <= 60:
                    to_del.append(n)
        for n in to_del:
            parent.remove(n)

        # find 'Payable par' label y
        y_label = None
        for n in list(parent):
            if n.tag.endswith("text") and "".join(n.itertext()).strip() == "Payable par":
                try:
                    x = float(n.attrib.get("x", "0"))
                    y = float(n.attrib.get("y", "0"))
                    if abs(x - x_ref) <= 60:
                        y_label = y
                        break
                except ValueError:
                    pass

        EXCLUDE = {"Monnaie", "Montant", "Compte / Payable à", "Référence", "Payable par", "Point de dépôt", "Récépissé", "Section paiement"}
        y_bottom = None
        if y_label is not None:
            for n in list(parent):
                if not n.tag.endswith("text"):
                    continue
                txt = "".join(n.itertext()).strip()
                if not txt or txt in EXCLUDE:
                    continue
                try:
                    x = float(n.attrib.get("x", "0"))
                    y = float(n.attrib.get("y", "0"))
                except ValueError:
                    continue
                if abs(x - x_ref) <= 60 and (y_label < y < y_label + 240):
                    y_bottom = max(y_bottom or y, y)

        base_start = (y_bottom + OFFSET_BELOW_PAYEE_BLOCK) if y_bottom is not None else (y_ref + FALLBACK_CLEARANCE)

        # avoid overlapping currency section
        start_y = base_start
        y_monnaie = None
        for n in list(parent):
            if n.tag.endswith("text") and "".join(node_text(n)).strip() == "Monnaie":
                try:
                    x = float(n.attrib.get("x", "0"))
                    y = float(n.attrib.get("y", "0"))
                except ValueError:
                    continue
                if abs(x - x_ref) <= 60:
                    y_monnaie = y
                    break
        if y_monnaie:
            cap = y_monnaie - 6.0
            start_y = min(start_y, cap - block_h())

        def new_text(ypos, txt, bold=False, size=FONT_SIZE):
            e = ET.Element(f"{{{NS['svg']}}}text", x=str(x_ref + x_shift), y=str(ypos))
            if bold:
                e.set("font-weight", "bold")
            e.set("font-size", str(size))
            e.text = txt
            return e

        parent.append(new_text(start_y, "Informations complémentaires", bold=True))
        y = start_y + LINE_GAP
        for s in (lines + ["", ""])[:3]:
            if s:
                parent.append(new_text(y, s))
            y += LINE_GAP

    def node_text(n):
        return "".join(n.itertext()) if n is not None else ""

    for _, (x_ref, y_ref, parent), shift in sides:
        inject_for_side(x_ref, y_ref, parent, shift)

    tree.write(str(svg_path), encoding="utf-8")


# ========= API =========
APP_NAME = "QR-Bill API (Meno)"
API_KEY = os.getenv("QRBILL_API_KEY", "")

ALLOWED_ORIGINS = [
    "https://meno-reinigung.ch",
    "https://www.meno-reinigung.ch",
]

app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)


class GeneratePayload(BaseModel):
    amount: str = Field(default="162.15")
    iban: str = Field(default="CH15 0076 8300 1685 0780 5")
    lang: str = Field(default="fr")

    creditor_name: str = Field(default="Meno Transport")
    creditor_zip: str = Field(default="1785")
    creditor_city: str = Field(default="CRESSIER")
    creditor_street: str = Field(default="Route de la Gare")
    creditor_house_no: str = Field(default="100")

    debtor_name: str = Field(default="King Jouet SA")
    debtor_street: str = Field(default="Centre Commercial Pam Center")
    debtor_zip: str = Field(default="1964")
    debtor_city: str = Field(default="Conthey")

    # Référence KJ format demandé
    mt_prefix: str = Field(default="MT00")
    year: Optional[int] = None
    client_code: str = Field(default="KJ00")
    company_code: str = Field(..., description="Code magasin 4 chiffres, ex 0960")
    invoice_no: str = Field(..., description="Numéro facture, ex 1929")

    # Bloc info imprimé
    info_company: str = Field(default="KING JOUET")
    info_line1: str = Field(default="Avenue Cardinal-Mermillod 36")
    info_line2: str = Field(default="1227 Carouge GE")


def require_api_key(x_api_key: Optional[str]):
    if not API_KEY:
        # si tu oublies de configurer la clé sur Render, on bloque
        raise HTTPException(status_code=500, detail="API key not configured on server (QRBILL_API_KEY).")
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized (bad API key).")


@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME}


@app.post("/generate")
def generate(payload: GeneratePayload, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)

    try:
        base = build_kj_base(
            company_code=payload.company_code,
            invoice_no=payload.invoice_no,
            year=payload.year,
            mt_prefix=payload.mt_prefix,
            client_code=payload.client_code,
        )
        rf_reference = rf_from_base(base)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Reference error: {e}")

    # Générer SVG via qrbill
    creditor_street = (payload.creditor_street or "").strip()
    creditor_house_no = (payload.creditor_house_no or "").strip()
    creditor_street_full = f"{creditor_street} {creditor_house_no}".strip()

    bill = QRBill(
        account=payload.iban,
        creditor={
            "name": payload.creditor_name,
            "street": creditor_street_full,   # ✅ IMPORTANT
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

    tmp_dir = Path("/tmp")
    svg_path = tmp_dir / "qr-bill.svg"
    out_pdf = tmp_dir / "qr-bill.pdf"

    bill.as_svg(str(svg_path), full_page=True)

    # Injecter infos complémentaires sans chevauchement
    printed_ref = prettify_groups4(rf_reference)
    inject_info_both_sides(svg_path, printed_ref, [
        (payload.info_company or "").strip(),
        (payload.info_line1 or "").strip(),
        (payload.info_line2 or "").strip(),
    ])

    drawing = svg2rlg(str(svg_path))
    renderPDF.drawToFile(drawing, str(out_pdf))

    pdf_bytes = out_pdf.read_bytes()

    # Nom de fichier propre
    yy = payload.year if payload.year else date.today().year
    filename = f"QRBill_{payload.company_code}_{payload.invoice_no}_{yy}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )
