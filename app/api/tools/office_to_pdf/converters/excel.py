from __future__ import annotations

import fitz
import pandas as pd
import camelot
import pdfplumber


def convert_to_excel(pdf_path: str, output_path: str) -> None:
    doc = fitz.open(pdf_path)
    try:
        total_pages = len(doc)
    finally:
        doc.close()

    all_extracted_dfs: list[pd.DataFrame] = []

    for page_num in range(1, total_pages + 1):
        extracted_tables_dfs: list[pd.DataFrame] = []

        try:
            lattice_tables = camelot.read_pdf(pdf_path, pages=str(page_num), flavor="lattice")
            if len(lattice_tables) > 0 and any(t.df.shape[1] > 1 for t in lattice_tables):
                for t in lattice_tables:
                    if t.df.shape[1] > 1:
                        extracted_tables_dfs.append(t.df)
        except Exception:
            pass

        if not extracted_tables_dfs:
            try:
                stream_tables = camelot.read_pdf(pdf_path, pages=str(page_num), flavor="stream")
                if len(stream_tables) > 0 and any(t.df.shape[1] > 1 for t in stream_tables):
                    for t in stream_tables:
                        if t.df.shape[1] > 1:
                            extracted_tables_dfs.append(t.df)
            except Exception:
                pass

        if not extracted_tables_dfs:
            try:
                strategy_strict = {
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                    "intersection_y_tolerance": 15,
                }
                strategy_loose = {
                    "vertical_strategy": "text",
                    "horizontal_strategy": "text",
                    "snap_tolerance": 5,
                    "join_tolerance": 5,
                }

                with pdfplumber.open(pdf_path) as pdf:
                    page = pdf.pages[page_num - 1]
                    table = page.extract_table(strategy_strict) or page.extract_table(strategy_loose)
                    if table:
                        df = pd.DataFrame([[cell for cell in row] for row in table if any(row)])
                        if df.shape[1] > 1:
                            extracted_tables_dfs.append(df)
            except Exception:
                pass

        if not extracted_tables_dfs:
            try:
                page_doc = fitz.open(pdf_path)
                try:
                    page_text = page_doc[page_num - 1].get_text("text").strip()
                finally:
                    page_doc.close()

                if page_text:
                    lines = [line.strip() for line in page_text.split("\n") if line.strip()]
                    processed_rows = []
                    for line in lines:
                        if "," in line:
                            processed_rows.append(line.split(","))
                        else:
                            processed_rows.append([line])

                    df = pd.DataFrame(processed_rows)
                    if not df.empty:
                        extracted_tables_dfs.append(df)
            except Exception:
                pass

        if extracted_tables_dfs:
            for df in extracted_tables_dfs:
                df.columns = range(df.shape[1])
                all_extracted_dfs.append(df)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        if all_extracted_dfs:
            master_df = pd.concat(all_extracted_dfs, ignore_index=True)
            master_df.to_excel(writer, sheet_name="All Rows", index=False, header=False)
        else:
            pd.DataFrame(["No tabular data detected on any page."]).to_excel(
                writer,
                sheet_name="Result",
                index=False,
            )
