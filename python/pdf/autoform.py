#!/usr/bin/env python3
"""
pdf_autoform.py – Automatisch ausfüllbare Felder zu einem PDF hinzufügen.

Erkennt drei Muster:
  1. Tabellenzellen  – aus vertikalen/horizontalen Linien rekonstruiert
  2. Punktlinien     – Sequenzen aus '…' Zeichen (typisch deutsche Formulare)
  3. Unterstriche    – Sequenzen aus '_' Zeichen

Usage:
  python pdf_autoform.py input.pdf output.pdf
  python pdf_autoform.py input.pdf output.pdf --debug   # zeigt erkannte Felder
"""

import sys
import argparse
import itertools
from pathlib import Path
import pdfplumber
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, DictionaryObject, FloatObject, NameObject,
    NumberObject, create_string_object,
)

# ── Konfiguration ─────────────────────────────────────────────────────────────

# Minimale Breite/Höhe damit ein erkanntes Element als Eingabefeld gilt
MIN_FIELD_WIDTH  = 20   # Punkte (~7mm)
MIN_FIELD_HEIGHT = 6    # Punkte (~2mm)

# Zellen mit mehr als dieser Höhe → multiline Textfeld
MULTILINE_THRESHOLD = 28  # Punkte (~1cm)

# Innerer Abstand vom erkannten Bereich zum Eingabefeld
PADDING = 2  # Punkte

# Maximale Höhe einer Tabellenzelle (höher = wahrscheinlich kein Formularfeld)
MAX_CELL_HEIGHT = 200


# ── PDF-Koordinaten ────────────────────────────────────────────────────────────

def to_pypdf_rect(x0, top_pl, x1, bot_pl, page_height):
    """
    Konvertiert pdfplumber-Koordinaten (y von oben) zu pypdf-Rect (y von unten).
    Wendet auch den inneren PADDING an.
    """
    return [
        x0   + PADDING,
        page_height - bot_pl  + PADDING,
        x1   - PADDING,
        page_height - top_pl  - PADDING,
    ]


# ── Feldererkennung ───────────────────────────────────────────────────────────

def detect_table_cells(page):
    """
    Rekonstruiert Tabellenzellen aus dem Liniennetz der Seite.
    Filtert Spalten heraus, die überwiegend Text enthalten (= Label-Spalten).
    Gibt Liste von (x0, top, x1, bottom) zurück.
    """
    rects = page.rects

    # Vertikale Linien: sehr schmale, hohe Rechtecke
    verts  = [r for r in rects if (r['x1'] - r['x0']) < 1.5 and (r['bottom'] - r['top']) > MIN_FIELD_HEIGHT]
    # Horizontale Linien: sehr flache, breite Rechtecke
    horizs = [r for r in rects if (r['bottom'] - r['top']) < 1.5 and (r['x1'] - r['x0']) > MIN_FIELD_WIDTH]

    if not verts or not horizs:
        return []

    # Eindeutige x- und y-Positionen (gerundet auf 1pt)
    vx = sorted(set(round((r['x0'] + r['x1']) / 2) for r in verts))
    hy = sorted(set(round((r['top'] + r['bottom']) / 2) for r in horizs))

    words = page.extract_words()
    row_pairs = list(zip(hy, hy[1:]))

    # Bestimme welche Spalten Label-Spalten sind (>30% der Zeilen haben Text)
    label_columns = set()
    for (x0, x1) in zip(vx, vx[1:]):
        filled = sum(
            1 for (y0, y1) in row_pairs
            if any(w['x0'] >= x0 - 2 and w['x1'] <= x1 + 2
                   and w['top'] >= y0 - 2 and w['bottom'] <= y1 + 2
                   for w in words)
        )
        if len(row_pairs) > 0 and (filled / len(row_pairs)) > 0.30:
            label_columns.add((x0, x1))

    cells = []
    for (x0, x1), (y0, y1) in itertools.product(
        zip(vx, vx[1:]), row_pairs
    ):
        if (x0, x1) in label_columns:
            continue  # Label-Spalte überspringen
        w = x1 - x0
        h = y1 - y0
        if w >= MIN_FIELD_WIDTH and MIN_FIELD_HEIGHT <= h <= MAX_CELL_HEIGHT:
            cells.append((x0, y0, x1, y1))

    return cells


def detect_dotted_lines(page):
    """
    Findet Punktlinien (……………) und Unterstrich-Linien (____________).
    Gibt Liste von (x0, top, x1, bottom) zurück.
    """
    words = page.extract_words(x_tolerance=1, y_tolerance=1)
    fields = []

    for w in words:
        text  = w['text'].strip()
        if len(text) < 4:
            continue

        is_dots        = all(c in '….' for c in text) and len(text) >= 4
        is_underscores = all(c == '_'  for c in text) and len(text) >= 4

        if is_dots or is_underscores:
            # Schätze Feldhöhe aus Zeichengröße (ca. 1.5× Zeilenhöhe)
            char_height = w['bottom'] - w['top']
            field_height = max(char_height * 1.1, MIN_FIELD_HEIGHT + 2)
            fields.append((
                w['x0'],
                w['top'],
                w['x1'],
                w['top'] + field_height,
            ))

    return fields


def merge_overlapping(fields, tolerance=3):
    """
    Entfernt doppelte oder sich stark überlappende Felder.
    """
    if not fields:
        return []

    merged = []
    used = set()

    for i, f1 in enumerate(fields):
        if i in used:
            continue
        x0, y0, x1, y1 = f1
        for j, f2 in enumerate(fields):
            if j <= i or j in used:
                continue
            ox0, oy0, ox1, oy1 = f2
            # Überlappt in x und y?
            overlap_x = min(x1, ox1) - max(x0, ox0)
            overlap_y = min(y1, oy1) - max(y0, oy0)
            if overlap_x > tolerance and overlap_y > tolerance:
                used.add(j)
        merged.append(f1)

    return merged


def detect_fields(page):
    """
    Kombiniert alle Erkennungsmethoden und gibt bereinigte Feldliste zurück.
    """
    cells  = detect_table_cells(page)
    dotted = detect_dotted_lines(page)

    all_fields = cells + dotted

    # Filterung: Felder die bereits in Tabellenzellen liegen,
    # werden nicht doppelt hinzugefügt
    if cells and dotted:
        filtered_dotted = []
        for df in dotted:
            dx0, dy0, dx1, dy1 = df
            inside_cell = any(
                cx0 <= dx0 and dx1 <= cx1 and cy0 <= dy0 and dy1 <= cy1
                for (cx0, cy0, cx1, cy1) in cells
            )
            if not inside_cell:
                filtered_dotted.append(df)
        all_fields = cells + filtered_dotted

    return merge_overlapping(all_fields)


# ── AcroForm / Feldgenerierung ────────────────────────────────────────────────

def setup_acroform(writer):
    if "/AcroForm" not in writer._root_object:
        af = DictionaryObject({
            NameObject("/Fields"):           ArrayObject(),
            NameObject("/DA"):               create_string_object("/Helv 9 Tf 0 g"),
            NameObject("/NeedAppearances"):  NameObject("/true"),
            NameObject("/DR"): DictionaryObject({
                NameObject("/Font"): DictionaryObject({
                    NameObject("/Helv"): DictionaryObject({
                        NameObject("/Type"):     NameObject("/Font"),
                        NameObject("/Subtype"):  NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    })
                })
            }),
        })
        writer._root_object[NameObject("/AcroForm")] = writer._add_object(af)
    return writer._root_object["/AcroForm"].get_object()


def add_text_field(writer, acroform, name, rect, page_idx, multiline=False):
    field = DictionaryObject({
        NameObject("/Type"):    NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/FT"):      NameObject("/Tx"),
        NameObject("/T"):       create_string_object(name),
        NameObject("/V"):       create_string_object(""),
        NameObject("/DA"):      create_string_object("/Helv 9 Tf 0 g"),
        NameObject("/Q"):       NumberObject(0),
        NameObject("/Ff"):      NumberObject(4096 if multiline else 0),
        NameObject("/Rect"):    ArrayObject([FloatObject(r) for r in rect]),
        NameObject("/MK"): DictionaryObject({
            NameObject("/BG"): ArrayObject([FloatObject(1), FloatObject(1), FloatObject(1)]),
            NameObject("/BC"): ArrayObject([FloatObject(0.5), FloatObject(0.5), FloatObject(0.5)]),
        }),
        NameObject("/BS"): DictionaryObject({
            NameObject("/W"): NumberObject(1),
            NameObject("/S"): NameObject("/S"),
        }),
    })

    page = writer.pages[page_idx]
    if "/Annots" not in page:
        page[NameObject("/Annots")] = ArrayObject()

    ref = writer._add_object(field)
    page[NameObject("/Annots")].append(ref)
    acroform["/Fields"].append(ref)
    return ref


# ── Hauptprogramm ─────────────────────────────────────────────────────────────

def process_pdf(input_path: str, output_path: str, debug: bool = False):
    total_fields = 0

    with pdfplumber.open(input_path) as plumber_pdf:
        reader = PdfReader(input_path)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        acroform = setup_acroform(writer)

        for page_idx, plumber_page in enumerate(plumber_pdf.pages):
            page_height = float(plumber_page.height)
            fields = detect_fields(plumber_page)

            if debug:
                print(f"\nSeite {page_idx + 1}: {len(fields)} Felder erkannt")

            for field_idx, (x0, top, x1, bottom) in enumerate(fields):
                h = bottom - top
                multiline = h > MULTILINE_THRESHOLD
                name = f"p{page_idx+1}_f{field_idx+1}"
                rect = to_pypdf_rect(x0, top, x1, bottom, page_height)

                # Sicherheitscheck: Rect muss gültig sein
                if rect[2] <= rect[0] or rect[3] <= rect[1]:
                    if debug:
                        print(f"  SKIP ungültiges Rect: {rect}")
                    continue

                add_text_field(writer, acroform, name, rect, page_idx, multiline)
                total_fields += 1

                if debug:
                    print(f"  [{name}] x={x0:.0f}-{x1:.0f} top={top:.0f}-{bottom:.0f}"
                          f" {'[multiline]' if multiline else ''}")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        writer.write(f)

    print(f"✓ {total_fields} Felder hinzugefügt → {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Fügt automatisch ausfüllbare Felder zu einem PDF hinzu."
    )
    parser.add_argument("input",  help="Pfad zur Eingabe-PDF")
    parser.add_argument("output", help="Pfad zur Ausgabe-PDF")
    parser.add_argument("--debug", action="store_true",
                        help="Zeigt erkannte Felder pro Seite")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"Fehler: Datei nicht gefunden: {args.input}", file=sys.stderr)
        sys.exit(1)

    process_pdf(args.input, args.output, debug=args.debug)


if __name__ == "__main__":
    main()