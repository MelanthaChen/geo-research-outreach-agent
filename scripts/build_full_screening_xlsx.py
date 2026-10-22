#!/usr/bin/env python3
"""Streaming XLSX fallback for the complete screening CSV export.

The bundled artifact runtime was attempted first but is not practical for this
72k-row CSV in the local environment. xlsxwriter's constant-memory mode keeps
the workbook complete without loading all rows into memory.
"""
import csv
from pathlib import Path

import xlsxwriter


source = Path("data/full_screening/full_us_outreach_screening.csv")
target = source.with_suffix(".xlsx")
workbook = xlsxwriter.Workbook(target, {"constant_memory": True, "strings_to_urls": False})
sheet = workbook.add_worksheet("Screening Results")
header_format = workbook.add_format({"bold": True, "font_color": "white", "bg_color": "#1F4E78", "text_wrap": True})
with source.open(encoding="utf-8-sig", newline="") as handle:
    reader = csv.reader(handle)
    for row_num, row in enumerate(reader):
        for col_num, value in enumerate(row):
            sheet.write(row_num, col_num, value, header_format if row_num == 0 else None)
sheet.freeze_panes(1, 0)
sheet.autofilter(0, 0, row_num, len(row) - 1)
sheet.set_row(0, 30)
sheet.set_column(0, 0, 30)
sheet.set_column(1, 1, 20)
sheet.set_column(2, 2, 28)
sheet.set_column(5, 5, 36)
sheet.set_column(13, 13, 38)
sheet.set_column(15, 15, 40)
sheet.set_column(16, 16, 24)
workbook.close()
print(target)
