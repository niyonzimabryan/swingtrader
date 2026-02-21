"""
PDF Generator — creates PDF reports from deep research findings.
Uses fpdf2 for lightweight PDF generation (no heavy dependencies).
"""

import os
import re
from datetime import datetime
from utils.logger import get_logger

log = get_logger("pdf_generator")


def generate_deep_research_pdf(
    ticker: str,
    research_report: str,
    scoring_result: dict = None,
    reevaluation: dict = None,
    output_dir: str = "reports",
) -> str:
    """
    Generate a PDF from a deep research report.

    Returns: path to generated PDF file, or "" on failure.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        log.warning("fpdf2 not installed. Run: pip install fpdf2")
        return ""

    try:
        os.makedirs(output_dir, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{ticker}_deep_research_{timestamp}.pdf"
        filepath = os.path.join(output_dir, filename)

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # Title
        pdf.set_font("Helvetica", "B", 18)
        pdf.cell(0, 12, f"Deep Research Report: {ticker}", ln=True, align="C")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 6, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}", ln=True, align="C")
        pdf.ln(8)

        # Scoring summary
        if scoring_result:
            pdf.set_font("Helvetica", "B", 12)
            pdf.cell(0, 8, "Scoring Summary", ln=True)
            pdf.set_font("Helvetica", "", 10)
            score = scoring_result.get("final_score", 0)
            classification = scoring_result.get("classification", "?")
            direction = scoring_result.get("direction", "?")
            pdf.cell(0, 6, f"Score: {score:.2f} | Classification: {classification} | Direction: {direction}", ln=True)
            pdf.ln(4)

        # Re-evaluation summary
        if reevaluation and reevaluation.get("recommendation"):
            pdf.set_font("Helvetica", "B", 12)
            pdf.cell(0, 8, "Opus Re-Evaluation", ln=True)
            pdf.set_font("Helvetica", "", 10)
            rec = reevaluation.get("recommendation", "?")
            new_score = reevaluation.get("final_score", "?")
            changed = reevaluation.get("recommendation_changed", False)
            status = "CHANGED" if changed else "CONFIRMED"
            pdf.cell(0, 6, f"Recommendation: {rec.upper()} ({status})", ln=True)
            pdf.cell(0, 6, f"Updated Score: {new_score}", ln=True)
            insight = reevaluation.get("key_insight_from_research", "")
            if insight:
                pdf.cell(0, 6, f"Key Insight: {insight[:200]}", ln=True)
            reasoning = reevaluation.get("reasoning", "")
            if reasoning:
                pdf.ln(2)
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(0, 5, _safe_text(f"Reasoning: {reasoning[:500]}"))
            pdf.set_x(pdf.l_margin)
            pdf.ln(4)

        # Research report (main content)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "Deep Research Report", ln=True)
        pdf.ln(2)

        # Parse and render the report with basic formatting
        _render_report(pdf, research_report)

        pdf.output(filepath)
        log.info("pdf_generated", ticker=ticker, path=filepath)
        return filepath

    except Exception as e:
        log.error("pdf_generation_failed", ticker=ticker, error=str(e))
        return ""


def _render_report(pdf, text: str):
    """Render research report text to PDF with basic formatting."""
    pdf.set_font("Helvetica", "", 10)
    left = pdf.l_margin  # Cache left margin for X reset after multi_cell

    # Split by lines and handle basic markdown-like formatting
    lines = text.split("\n")
    for line in lines:
        stripped = line.strip()

        if not stripped:
            pdf.ln(3)
            continue

        # Reset X to left margin before each line (fpdf2 multi_cell quirk)
        pdf.set_x(left)

        # Headers (# or ## or ###)
        if stripped.startswith("### "):
            pdf.set_font("Helvetica", "B", 11)
            pdf.multi_cell(0, 6, _safe_text(stripped[4:]))
            pdf.set_font("Helvetica", "", 10)
        elif stripped.startswith("## "):
            pdf.set_font("Helvetica", "B", 12)
            pdf.multi_cell(0, 7, _safe_text(stripped[3:]))
            pdf.set_font("Helvetica", "", 10)
        elif stripped.startswith("# "):
            pdf.set_font("Helvetica", "B", 14)
            pdf.multi_cell(0, 8, _safe_text(stripped[2:]))
            pdf.set_font("Helvetica", "", 10)
        elif stripped.startswith("- ") or stripped.startswith("* "):
            # Bullet point — use dash instead of unicode bullet for latin-1 compat
            bullet_text = f"  - {stripped[2:]}"
            pdf.multi_cell(0, 5, _safe_text(bullet_text))
        elif stripped.startswith("**") and stripped.endswith("**"):
            # Bold line
            clean = stripped.strip("*").strip()
            pdf.set_font("Helvetica", "B", 10)
            pdf.multi_cell(0, 5, _safe_text(clean))
            pdf.set_font("Helvetica", "", 10)
        else:
            # Regular text — strip markdown bold/italic markers
            clean = re.sub(r'\*\*(.*?)\*\*', r'\1', stripped)
            clean = re.sub(r'\*(.*?)\*', r'\1', clean)
            pdf.multi_cell(0, 5, _safe_text(clean))


def _safe_text(text: str) -> str:
    """Encode text to latin-1 safe form for fpdf Helvetica font."""
    try:
        return text.encode('latin-1', 'replace').decode('latin-1')
    except Exception:
        return text.encode('ascii', 'replace').decode('ascii')
