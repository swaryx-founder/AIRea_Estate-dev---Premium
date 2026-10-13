"""Convert API.md to API.pdf (one-off utility)."""
from pathlib import Path

import markdown
from xhtml2pdf import pisa

ROOT = Path(__file__).resolve().parent.parent
MD_PATH = ROOT / "API.md"
PDF_PATH = ROOT / "API.pdf"

CSS = """
body { font-family: Arial, sans-serif; font-size: 11px; line-height: 1.45; margin: 28px; color: #111; }
h1 { font-size: 22px; border-bottom: 2px solid #222; padding-bottom: 8px; }
h2 { font-size: 16px; margin-top: 20px; color: #222; }
h3 { font-size: 13px; margin-top: 14px; }
h4 { font-size: 12px; }
code { font-family: Consolas, monospace; font-size: 9px; background: #f5f5f5; padding: 1px 3px; }
pre { font-family: Consolas, monospace; font-size: 9px; background: #f5f5f5; padding: 10px; white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 10px; }
th, td { border: 1px solid #bbb; padding: 5px 7px; text-align: left; vertical-align: top; }
th { background: #eee; font-weight: bold; }
hr { border: none; border-top: 1px solid #ddd; margin: 18px 0; }
a { color: #0066cc; text-decoration: none; }
ul, ol { padding-left: 20px; }
"""


def main() -> None:
    md_text = MD_PATH.read_text(encoding="utf-8")
    html_body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "nl2br"],
    )
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>OutboundAI API Reference</title>
  <style>{CSS}</style>
</head>
<body>{html_body}</body>
</html>"""

    with PDF_PATH.open("w+b") as pdf_file:
        status = pisa.CreatePDF(html, dest=pdf_file, encoding="utf-8")

    if status.err:
        raise SystemExit(f"PDF generation failed with {status.err} errors")
    print(f"Created: {PDF_PATH}")


if __name__ == "__main__":
    main()
