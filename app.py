#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import io
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Response, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from PIL import Image
from qrbill import QRBill
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPDF, renderPM
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader


# ========= Réglages injection (anti-superposition) =========
OFFSET_BELOW_PAYEE_BLOCK = 22
FALLBACK_CLEARANCE = 20
FONT_SIZE = 10
LINE_GAP = 12
SHIFT_X_LEFT = 0
SHIFT_X_RIGHT = 0

# ========= Logo facture =========
BASE_DIR = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "meno-transport-logo.png"
LOGO_WIDTH = 32 * mm
LOGO_HEIGHT = 32 * mm


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
        ref_labels = {"Référence", "Referenz", "Reference", "Riferimento"}
        for parent in root.iter():
            for node in list(parent):
                if node.tag.endswith("text") and "".join(node.itertext()).strip() in ref_labels:
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

    title_size = 8.6
    normal_size = 7.8
    contact_size = 7.0
    title_gap = 10.0
    line_gap = 9.0

    payable_labels = {"Payable par", "Zahlbar durch", "Payable by", "Pagabile da"}
    ref_labels = {"Référence", "Referenz", "Reference", "Riferimento"}
    money_labels = {"Monnaie", "Währung", "Currency", "Valuta"}

    excluded_labels = {
        "Monnaie", "Montant", "Compte / Payable à", "Référence",
        "Payable par", "Point de dépôt", "Récépissé", "Section paiement",
        "Währung", "Betrag", "Konto / Zahlbar an", "Referenz",
        "Zahlbar durch", "Annahmestelle", "Empfangsschein", "Zahlteil",
        "Currency", "Amount", "Account / Payable to", "Reference",
        "Payable by", "Receipt", "Payment part",
        "Valuta", "Importo", "Conto / Pagabile a", "Riferimento",
        "Pagabile da", "Ricevuta", "Sezione pagamento",
        "Informations complémentaires"
    }

    content_lines = [(s or "").strip() for s in (lines + ["", "", "", ""])[:4]]
    visible_lines = [s for s in content_lines if s]

    def block_h():
        h = title_size + title_gap
        for idx, s in enumerate(content_lines):
            if s:
                h += line_gap
        return h

    def node_text(n):
        return "".join(n.itertext()) if n is not None else ""

    def new_text(x, y, txt, bold=False, size=normal_size):
        e = ET.Element(f"{{{NS['svg']}}}text", x=str(x), y=str(y))
        if bold:
            e.set("font-weight", "bold")
        e.set("font-size", str(size))
        e.text = txt
        return e

    def inject_for_side(side_name, x_ref, y_ref, parent, x_shift):
        # supprime les anciennes lignes injectées près de cette colonne
        to_del = []
        for n in list(parent):
            if not n.tag.endswith("text"):
                continue
            txt = "".join(n.itertext()).strip()
            if txt and (txt == "Informations complémentaires" or txt in visible_lines):
                try:
                    x = float(n.attrib.get("x", "0"))
                except ValueError:
                    continue
                if abs(x - x_ref) <= 80:
                    to_del.append(n)
        for n in to_del:
            parent.remove(n)

        # trouve la zone "payable par"
        y_label = None
        for n in list(parent):
            if n.tag.endswith("text") and "".join(n.itertext()).strip() in payable_labels:
                try:
                    x = float(n.attrib.get("x", "0"))
                    y = float(n.attrib.get("y", "0"))
                    if abs(x - x_ref) <= 80:
                        y_label = y
                        break
                except ValueError:
                    pass

        # calcule le bas réel du bloc débiteur
        y_bottom = None
        if y_label is not None:
            for n in list(parent):
                if not n.tag.endswith("text"):
                    continue
                txt = "".join(n.itertext()).strip()
                if not txt or txt in excluded_labels:
                    continue
                try:
                    x = float(n.attrib.get("x", "0"))
                    y = float(n.attrib.get("y", "0"))
                except ValueError:
                    continue
                if abs(x - x_ref) <= 80 and (y_label < y < y_label + 320):
                    y_bottom = max(y_bottom or y, y)

        # position de départ : plus basse sous le bloc payable
        if y_bottom is not None:
            start_y = y_bottom + 26.0
        else:
            start_y = y_ref + 30.0

        # limite haute de la zone monnaie/montant
        y_monnaie = None
        for n in list(parent):
            if n.tag.endswith("text") and "".join(node_text(n)).strip() in money_labels:
                try:
                    x = float(n.attrib.get("x", "0"))
                    y = float(n.attrib.get("y", "0"))
                except ValueError:
                    continue
                if abs(x - x_ref) <= 80:
                    y_monnaie = y
                    break

        if y_monnaie:
            is_left = side_name == "left" or x_ref < 300
            cap = y_monnaie - (24.0 if is_left else 12.0)
            start_y = min(start_y, cap - block_h())

        # sécurité minimale : ne pas remonter trop haut non plus
        if y_bottom is not None:
            start_y = max(start_y, y_bottom + 16.0)

        x_text = x_ref + x_shift

        # titre
        parent.append(new_text(x_text, start_y, "Informations complémentaires", bold=True, size=title_size))

        # lignes
        y = start_y + title_gap
        for idx, s in enumerate(content_lines):
            if s:
                size = contact_size if idx == 3 else normal_size
                parent.append(new_text(x_text, y, s, bold=False, size=size))
            y += line_gap

    for side_name, (x_ref, y_ref, parent), shift in sides:
        inject_for_side(side_name, x_ref, y_ref, parent, shift)

    tree.write(str(svg_path), encoding="utf-8")


# ========= Génération section basse =========
def render_bottom_svg(bill: QRBill, path: Path):
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


def svg_to_highres_png(svg_path: Path, png_path: Path, dpi: int = 450):
    drawing = svg2rlg(str(svg_path))
    if drawing is None:
        raise ValueError("Impossible de lire le SVG pour conversion PNG.")

    png_bytes = renderPM.drawToString(drawing, fmt="PNG", dpi=dpi)

    img = Image.open(io.BytesIO(png_bytes))
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")

    img.save(str(png_path), format="PNG", optimize=False)



# ========= Facture complète d'essai =========
def money(value) -> Decimal:
    try:
        text = str(value or "0").strip().replace("'", "").replace(" ", "").replace(",", ".")
        return Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Montant invalide: {value}")


def weight(value) -> Decimal:
    try:
        text = str(value or "0").strip().replace("'", "").replace(" ", "").replace(",", ".")
        return Decimal(text).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f"Poids invalide: {value}")


def fmt_money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f} CHF"


def fmt_weight(value: Decimal) -> str:
    if value == 0:
        return "---"
    return f"{value.quantize(Decimal('0.001')):.3f} T".replace(".", ",")


def wrap_text(c, text: str, x: float, y: float, max_width: float, font="Helvetica", size=8.2, leading=9.3, max_lines=3):
    words = (text or "").split()
    lines, current = [], ""
    for word in words:
        trial = word if not current else current + " " + word
        if c.stringWidth(trial, font, size) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= max_lines - 1:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    c.setFont(font, size)
    for line in lines:
        c.drawString(x, y, line)
        y -= leading
    return y


def invoice_labels(lang: str):
    lang = (lang or "fr").lower()
    data = {
        "fr": {"invoice":"Facture", "invoice_no":"Numéro de la facture", "reference":"Référence", "payable":"Payable par", "extra":"Informations complémentaires", "invoice_date":"Date de la facture", "service_date":"Date de livraison/prestation", "pos":"Pos.", "description":"Désignation", "qty":"Quantité", "unit":"Unité", "total":"Prix total", "net":"Montant net", "vat":"TVA", "grand":"Montant total de la facture"},
        "de": {"invoice":"Rechnung", "invoice_no":"Rechnungsnummer", "reference":"Referenz", "payable":"Zahlbar durch", "extra":"Zusätzliche Informationen", "invoice_date":"Rechnungsdatum", "service_date":"Liefer-/Leistungsdatum", "pos":"Pos.", "description":"Bezeichnung", "qty":"Menge", "unit":"Einheit", "total":"Gesamtpreis", "net":"Nettobetrag", "vat":"MwSt.", "grand":"Rechnungsbetrag"},
        "en": {"invoice":"Invoice", "invoice_no":"Invoice number", "reference":"Reference", "payable":"Payable by", "extra":"Additional information", "invoice_date":"Invoice date", "service_date":"Delivery/service date", "pos":"Pos.", "description":"Description", "qty":"Quantity", "unit":"Unit", "total":"Total price", "net":"Net amount", "vat":"VAT", "grand":"Invoice total"},
        "it": {"invoice":"Fattura", "invoice_no":"Numero fattura", "reference":"Riferimento", "payable":"Pagabile da", "extra":"Informazioni complementari", "invoice_date":"Data fattura", "service_date":"Data consegna/prestazione", "pos":"Pos.", "description":"Descrizione", "qty":"Quantità", "unit":"Unità", "total":"Prezzo totale", "net":"Importo netto", "vat":"IVA", "grand":"Totale fattura"},
    }
    return data.get(lang, data["fr"])


def draw_invoice_footer(c, page_w: float):
    """Dessine le pied de page Meno Transport sous le QR-Bill.

    La zone est volontairement compacte pour rester sur une seule page A4
    et reproduire la structure du PDF de référence.
    """
    footer_y = 6.0 * mm
    separator_y = 23.0 * mm
    left_x = 18.0 * mm
    center_x = page_w / 2
    right_x = page_w - 18.0 * mm

    # Ligne de séparation au-dessus du pied de page
    c.saveState()
    c.setStrokeColor(colors.HexColor("#8A8A8A"))
    c.setLineWidth(0.45)
    c.line(left_x, separator_y, right_x, separator_y)

    # Bloc gauche
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 6.6)
    c.drawString(left_x, footer_y + 13.0*mm, "Meno Transport Reinigung")
    c.drawString(left_x, footer_y + 9.8*mm, "YILMAZ Enis")
    c.setFont("Helvetica", 6.4)
    c.drawString(left_x, footer_y + 6.6*mm, "Route de la Gare 100,")
    c.drawString(left_x, footer_y + 3.4*mm, "1785 CRESSIER FR")

    # Bloc central
    c.setFont("Helvetica-Bold", 6.6)
    c.drawCentredString(center_x, footer_y + 13.0*mm, "info@meno-transport.ch")
    c.drawCentredString(center_x, footer_y + 9.8*mm, "UID : CHE-203.265.932")
    c.drawCentredString(center_x, footer_y + 6.6*mm, "CH-217.3.577.844-5")

    # Bloc droit
    c.setFont("Helvetica-Bold", 6.6)
    c.drawRightString(right_x, footer_y + 13.0*mm, "meno-transport.ch")
    c.setFont("Helvetica", 6.4)
    c.drawRightString(right_x, footer_y + 9.8*mm, "+41 76 270 16 76")
    c.drawRightString(right_x, footer_y + 6.6*mm, "+41 78 225 52 52")
    c.drawRightString(right_x, footer_y + 3.4*mm, "+41 79 506 36 43")
    c.restoreState()


def build_invoice_pdf(payload, bill: QRBill, rf_reference: str, out_pdf: Path, tmp_dir: Path):
    labels = invoice_labels(payload.lang)

    flat = money(payload.invoice_flat_fee)
    toys_w, toys_rate = weight(payload.invoice_toys_weight), money(payload.invoice_toys_rate)
    wood_w, wood_rate = weight(payload.invoice_wood_weight), money(payload.invoice_wood_rate)
    household_w, household_rate = weight(payload.invoice_household_weight), money(payload.invoice_household_rate)

    toys_total = (toys_w * toys_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    wood_total = (wood_w * wood_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    household_total = (household_w * household_rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    net = flat + toys_total + wood_total + household_total
    vat_rate = money(payload.invoice_vat_rate)
    vat = (net * vat_rate / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    grand = net + vat

    # QR-Bill avec le total TTC exact
    creditor_street_full = f"{payload.creditor_street} {payload.creditor_house_no}".strip()
    invoice_bill = QRBill(
        account=payload.iban,
        creditor={"name": payload.creditor_name, "street": creditor_street_full, "pcode": payload.creditor_zip, "city": payload.creditor_city, "country": "CH"},
        amount=f"{grand:.2f}",
        reference_number=rf_reference,
        debtor={"name": payload.debtor_name, "street": payload.debtor_street, "pcode": payload.debtor_zip, "city": payload.debtor_city, "country": "CH"},
        language=payload.lang,
    )

    qr_svg = tmp_dir / "invoice-qr-bottom.svg"
    render_bottom_svg(invoice_bill, qr_svg)
    inject_info_both_sides(qr_svg, prettify_groups4(rf_reference), [
        (payload.info_company or "").strip(),
        (payload.info_line1 or "").strip(),
        (payload.info_line2 or "").strip(),
        (payload.info_contact or "").strip(),
    ])
    qr_drawing = svg2rlg(str(qr_svg))
    if qr_drawing is None:
        raise ValueError("Impossible de convertir la partie QR-Bill.")

    page_w, page_h = A4
    c = canvas.Canvas(str(out_pdf), pagesize=A4)
    c.setTitle(f"Facture {payload.invoice_no}")

    margin = 18 * mm
    # Réserve la hauteur réglementaire de la partie basse du QR-Bill,
    # avec une marge minimale pour favoriser une sortie sur une seule page.
    # Le QR-Bill reste prioritaire sur la même page.
    # On réserve une petite bande de 24 mm en bas pour le pied de page,
    # puis on place le QR-Bill juste au-dessus.
    footer_reserved_h = 24 * mm
    qr_target_h = 92 * mm
    qr_y = footer_reserved_h
    invoice_bottom = qr_y + qr_target_h + 1.5 * mm

    # En-tête
    c.setFont("Helvetica-Bold", 8)
    c.drawString(margin, page_h - 16*mm, f"{payload.creditor_name}, {creditor_street_full}, {payload.creditor_zip} {payload.creditor_city}")

    # Logo Meno Transport : rapproché du bloc de gauche, comme sur la facture 2125.
    # Il se place juste après les coordonnées / informations complémentaires,
    # sans modifier la hauteur réservée à la facture.
    if LOGO_PATH.exists():
        try:
            logo_x = 77 * mm
            logo_y = page_h - 61 * mm
            c.drawImage(
                ImageReader(str(LOGO_PATH)),
                logo_x,
                logo_y,
                width=LOGO_WIDTH,
                height=LOGO_HEIGHT,
                preserveAspectRatio=True,
                mask="auto",
                anchor="c",
            )
        except Exception as e:
            print(f"[WARN] Impossible d'afficher le logo facture: {e}")
    else:
        print(f"[WARN] Logo facture introuvable: {LOGO_PATH}")

    y = page_h - 28*mm
    c.setFont("Helvetica-Bold", 10)
    c.drawString(margin, y, labels["payable"] + " :")
    c.setFont("Helvetica", 9.2)
    y -= 5*mm
    for line in [payload.debtor_name, payload.debtor_street, f"{payload.debtor_zip} {payload.debtor_city}"]:
        c.drawString(margin, y, line)
        y -= 4.3*mm

    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(margin, y-1*mm, labels["extra"] + " :")
    c.setFont("Helvetica", 8.8)
    y -= 6*mm
    for line in [payload.info_company, payload.info_line1, payload.info_line2]:
        if line:
            c.drawString(margin, y, line)
            y -= 4.1*mm

    right_x = page_w - 78*mm
    y2 = page_h - 18*mm
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(right_x, y2, labels["invoice_date"] + " :")
    c.setFont("Helvetica", 9.2)
    c.drawRightString(page_w-margin, y2, payload.invoice_date)

    # Date de livraison / prestation : le libellé sur une ligne, la période juste en dessous
    y2 -= 7*mm
    c.setFont("Helvetica-Bold", 9.5)
    c.drawString(right_x, y2, labels["service_date"] + " :")
    c.setFont("Helvetica", 9.2)
    c.drawRightString(page_w-margin, y2-4.5*mm, f"{payload.invoice_service_start} – {payload.invoice_service_end}")
    y2 -= 13.5*mm
    c.setFont("Helvetica-Bold", 9.5)
    c.drawRightString(page_w-margin, y2, payload.creditor_name)
    c.setFont("Helvetica", 9)
    c.drawRightString(page_w-margin, y2-4.5*mm, f"{payload.creditor_zip} {payload.creditor_city}")
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(page_w-margin, y2-9*mm, f"N° TVA: {payload.invoice_vat_number}")
    c.setFont("Helvetica", 8.8)
    c.drawRightString(page_w-margin, y2-13.5*mm, payload.iban)

    # Titre et référence
    # Rapproché du bloc d'informations pour gagner de la place verticale
    title_y = page_h - 73*mm
    c.setFont("Helvetica-Bold", 18)
    c.drawString(margin, title_y, labels["invoice"])
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(margin, title_y-9*mm, f"{labels['invoice_no']} : {payload.invoice_no}")
    c.setFont("Helvetica", 10.5)
    c.drawRightString(page_w-margin, title_y-9*mm, f"{labels['reference']}: {prettify_groups4(rf_reference)}")

    # Tableau
    # Colonnes rééquilibrées : davantage de place pour les valeurs à droite.
    # La désignation est volontairement un peu plus étroite et peut passer sur 2 lignes.
    table_top = title_y - 15.5*mm
    col_x = [
        margin,
        margin + 14*mm,   # Pos.          14 mm
        margin + 102*mm,  # Désignation   88 mm
        margin + 125*mm,  # Quantité      23 mm
        margin + 147*mm,  # Unité         22 mm
        page_w - margin,   # Prix total    27 mm
    ]
    header_h = 7*mm
    row_h = 9.8*mm
    rows = [
        ("1.", payload.invoice_desc_flat, "Illimité", "1", fmt_money(flat)),
        ("2.", payload.invoice_desc_empty, "---", "Gratuit", "0.00 CHF"),
        ("3.", payload.invoice_desc_toys, fmt_weight(toys_w), f"{toys_rate:.2f}/T", fmt_money(toys_total)),
        ("4.", payload.invoice_desc_wood, fmt_weight(wood_w), f"{wood_rate:.2f}/T", fmt_money(wood_total)),
    ]

    # Ligne ordures ménagères uniquement si une quantité > 0 est saisie
    if household_w > 0:
        rows.append(
            ("5.", payload.invoice_desc_household, fmt_weight(household_w), f"{household_rate:.2f}/T", fmt_money(household_total))
        )
    total_table_h = header_h + row_h*len(rows)
    table_bottom = table_top-total_table_h

    c.setStrokeColor(colors.black)
    c.setLineWidth(0.5)
    c.rect(margin, table_bottom, page_w-2*margin, total_table_h)
    for x in col_x[1:-1]:
        c.line(x, table_bottom, x, table_top)
    c.line(margin, table_top-header_h, page_w-margin, table_top-header_h)
    for i in range(1, len(rows)):
        c.line(margin, table_top-header_h-i*row_h, page_w-margin, table_top-header_h-i*row_h)

    c.setFont("Helvetica-Bold", 8.2)
    headers=[labels['pos'], labels['description'], labels['qty'], labels['unit'], labels['total']]
    for i, h in enumerate(headers):
        if i == 4:
            # Le titre de la dernière colonne reste bien à l'intérieur de la cellule.
            c.drawRightString(col_x[5]-2*mm, table_top-5.5*mm, h)
        else:
            c.drawString(col_x[i]+2*mm, table_top-5.5*mm, h)

    for r, row in enumerate(rows):
        ry = table_top-header_h-r*row_h-4.3*mm
        c.setFont("Helvetica", 8.0)
        c.drawString(col_x[0]+2*mm, ry, row[0])

        # Désignation : largeur réduite, 2 lignes maximum pour préserver la hauteur globale.
        wrap_text(
            c, row[1],
            col_x[1]+1.5*mm, ry,
            col_x[2]-col_x[1]-3*mm,
            size=7.2, leading=7.8, max_lines=2
        )

        c.setFont("Helvetica", 7.8)
        c.drawString(col_x[2]+2*mm, ry, row[2])
        c.drawString(col_x[3]+2*mm, ry, row[3])

        # Prix total aligné à droite avec une marge interne : aucun montant ne sort du tableau.
        c.drawRightString(col_x[5]-2*mm, ry, row[4])

    # Totaux
    totals_w=80*mm; totals_x=page_w-margin-totals_w; totals_y=table_bottom-24*mm
    c.rect(totals_x, totals_y, totals_w, 24*mm)
    c.line(totals_x+52*mm, totals_y, totals_x+52*mm, totals_y+24*mm)
    c.line(totals_x, totals_y+8*mm, totals_x+totals_w, totals_y+8*mm)
    c.line(totals_x, totals_y+16*mm, totals_x+totals_w, totals_y+16*mm)
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(totals_x+50*mm, totals_y+19*mm, labels['net']+" :")
    c.drawRightString(totals_x+50*mm, totals_y+11*mm, f"+ {labels['vat']} {vat_rate:.1f} %")
    c.drawRightString(totals_x+50*mm, totals_y+3*mm, labels['grand']+" :")
    c.setFont("Helvetica", 9)
    c.drawString(totals_x+54*mm, totals_y+19*mm, fmt_money(net))
    c.setFont("Helvetica-Bold", 9)
    c.drawString(totals_x+54*mm, totals_y+11*mm, fmt_money(vat))
    c.drawString(totals_x+54*mm, totals_y+3*mm, fmt_money(grand))

    # Vérifie que le contenu tient; sinon QR sur page 2.
    # Le pied de page est toujours dessiné sur la page qui contient le QR-Bill.
    one_page = totals_y >= invoice_bottom + 1.5*mm
    if one_page:
        scale = min(page_w/qr_drawing.width, qr_target_h/qr_drawing.height)
        qr_drawing.scale(scale, scale)
        renderPDF.draw(qr_drawing, c, 0, qr_y)
        draw_invoice_footer(c, page_w)
    else:
        c.showPage()
        # Même composition sur la page 2 : QR-Bill au-dessus du footer.
        scale = min(page_w/qr_drawing.width, qr_target_h/qr_drawing.height)
        qr_drawing.scale(scale, scale)
        renderPDF.draw(qr_drawing, c, 0, qr_y)
        draw_invoice_footer(c, page_w)

    c.save()
    return grand, net, vat, one_page

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
    info_contact: str = Field(default="")

    # Données facture complète (version d'essai)
    invoice_date: str = Field(default_factory=lambda: date.today().strftime("%d.%m.%Y"))
    invoice_service_start: str = Field(default="01.06.2026")
    invoice_service_end: str = Field(default="30.06.2026")
    invoice_vat_rate: str = Field(default="8.1")
    invoice_vat_number: str = Field(default="203.265.932")
    invoice_flat_fee: str = Field(default="180.00")
    invoice_toys_weight: str = Field(default="0")
    invoice_toys_rate: str = Field(default="250.00")
    invoice_wood_weight: str = Field(default="0")
    invoice_wood_rate: str = Field(default="100.00")
    invoice_household_weight: str = Field(default="0")
    invoice_household_rate: str = Field(default="250.00")
    invoice_desc_flat: str = Field(default="Forfait de nettoyage de palettes cassées, déchets de palettes en bois et jouets cassés.")
    invoice_desc_empty: str = Field(default="Forfait de nettoyage de palettes vides.")
    invoice_desc_toys: str = Field(default="Facturation au poids des jouets cassés.")
    invoice_desc_wood: str = Field(default="Facturation au poids des déchets bois (palettes cassées et déchets bois).")
    invoice_desc_household: str = Field(default="Facturation des ordures ménagères.")

    # Format de sortie
    output_format: str = Field(default="pdf", description="pdf, png_bottom ou invoice_pdf_test")


def require_api_key(x_api_key: Optional[str]):
    if not API_KEY:
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

    try:
        creditor_street = (payload.creditor_street or "").strip()
        creditor_house_no = (payload.creditor_house_no or "").strip()
        creditor_street_full = f"{creditor_street} {creditor_house_no}".strip()

        bill = QRBill(
            account=payload.iban,
            creditor={
                "name": payload.creditor_name,
                "street": creditor_street_full,
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
        yy = payload.year if payload.year else date.today().year

        # ===== PDF A4 =====
        if payload.output_format == "pdf":
            svg_path = tmp_dir / "qr-bill.svg"
            out_pdf = tmp_dir / "qr-bill.pdf"

            bill.as_svg(str(svg_path), full_page=True)

            printed_ref = prettify_groups4(rf_reference)
            inject_info_both_sides(svg_path, printed_ref, [
                (payload.info_company or "").strip(),
                (payload.info_line1 or "").strip(),
                (payload.info_line2 or "").strip(),
                (payload.info_contact or "").strip(),
            ])

            drawing = svg2rlg(str(svg_path))
            if drawing is None:
                raise ValueError("Impossible de convertir le SVG complet en drawing.")

            renderPDF.drawToFile(drawing, str(out_pdf))
            pdf_bytes = out_pdf.read_bytes()

            filename = f"QRBill_{payload.company_code}_{payload.invoice_no}_{yy}.pdf"

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'}
            )

        # ===== FACTURE COMPLÈTE + QR-BILL (ESSAI) =====
        elif payload.output_format == "invoice_pdf_test":
            out_pdf = tmp_dir / "invoice-test.pdf"
            grand, net, vat, one_page = build_invoice_pdf(payload, bill, rf_reference, out_pdf, tmp_dir)
            pdf_bytes = out_pdf.read_bytes()
            filename = f"Facture_TEST_{payload.company_code}_{payload.invoice_no}_{yy}.pdf"
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "X-Invoice-Total": f"{grand:.2f}",
                    "X-Invoice-One-Page": "1" if one_page else "0",
                },
            )

        # ===== PNG BAS =====
        elif payload.output_format == "png_bottom":
            svg_path = tmp_dir / "qr-bill-bottom.svg"
            out_png = tmp_dir / "qr-bill-bottom.png"

            render_bottom_svg(bill, svg_path)

            # Injection non bloquante des infos complémentaires
            try:
                printed_ref = prettify_groups4(rf_reference)
                inject_info_both_sides(svg_path, printed_ref, [
                    (payload.info_company or "").strip(),
                    (payload.info_line1 or "").strip(),
                    (payload.info_line2 or "").strip(),
                    (payload.info_contact or "").strip(),
                ])
            except Exception as e:
                print(f"[WARN] inject_info_both_sides on PNG failed: {e}")

            svg_to_highres_png(svg_path, out_png, dpi=450)

            png_bytes = out_png.read_bytes()
            filename = f"QRBill_BOTTOM_{payload.company_code}_{payload.invoice_no}_{yy}.png"

            return Response(
                content=png_bytes,
                media_type="image/png",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'}
            )

        else:
            raise HTTPException(status_code=400, detail="output_format invalide. Utiliser 'pdf', 'png_bottom' ou 'invoice_pdf_test'.")

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] /generate failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
